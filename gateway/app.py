import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from . import __version__
from .config import Settings
from .contracts import (
    ChatRequest,
    ResponseRequest,
    chat_finish_reason,
    chat_to_response,
    chat_usage,
    response_to_chat,
)
from .errors import GatewayError
from .network import client_ip
from .providers import ChatGPTProvider, OllamaProvider
from .selection import default_alias, resolve_request
from .storage import Storage

LOG = logging.getLogger("passerelle")


def json_sse(value, event=None):
    prefix = ("event: " + event + "\n") if event else ""
    return (
        prefix + "data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    ).encode()


def safe_error(exc):
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return GatewayError(504, "upstream_timeout", "Délai de réponse dépassé.")
    if isinstance(exc, httpx.HTTPError):
        return GatewayError(503, "backend_unavailable", "Backend injoignable.")
    return GatewayError(502, "invalid_upstream_response", "Réponse du backend inexploitable.")


class RateLimiter:
    def __init__(self):
        self.buckets = OrderedDict()

    def check(self, identity, limit):
        now = time.monotonic()
        queue = self.buckets.pop(identity, deque())
        while queue and queue[0] <= now - 60:
            queue.popleft()
        self.buckets[identity] = queue
        while len(self.buckets) > 4096:
            self.buckets.popitem(last=False)
        if len(queue) >= limit:
            raise GatewayError(429, "rate_limit_exceeded", "Trop de requêtes ; réessayez dans une minute.")
        queue.append(now)


class Guard:
    def __init__(self, app, settings, storage):
        self.app, self.settings, self.storage = app, settings, storage
        self.rate = RateLimiter()
        self.body_requests = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = "req_" + uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        headers = dict(scope.get("headers", []))
        started = time.monotonic()
        status = 500
        body_reserved = False

        async def wrapped_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] += [
                    (b"x-request-id", request_id.encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)

        try:
            try:
                host = urlsplit("//" + headers.get(b"host", b"").decode("latin1")).hostname
            except ValueError:
                host = None
            if host not in self.settings.allowed_hosts:
                raise GatewayError(400, "invalid_host", "Nom d'hôte non autorisé.")
            ip = scope.get("client", ("unknown", 0))[0]
            ip = client_ip(
                ip, headers.get(b"x-forwarded-for", b"").decode("latin1"), self.settings.trusted_proxy_cidrs
            )
            self.rate.check("ip:" + ip, self.settings.ip_rpm)
            origin = headers.get(b"origin", b"").decode("latin1")
            if origin and origin not in self.settings.allowed_origins:
                raise GatewayError(403, "origin_not_allowed", "Origine navigateur non autorisée.")
            if scope["path"] != "/healthz" and scope["method"] != "OPTIONS":
                bearer = headers.get(b"authorization", b"").decode("latin1")
                raw = bearer[7:] if bearer.lower().startswith("bearer ") else ""
                key = await asyncio.to_thread(self.storage.authenticate, raw)
                scope["state"]["key"] = key
                self.rate.check("key:" + key["id"], self.settings.key_rpm)
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                if headers.get(b"content-encoding", b"identity") != b"identity":
                    raise GatewayError(415, "unsupported_encoding", "Corps compressés non acceptés.")
                if headers.get(b"content-type", b"").split(b";", 1)[0].lower() != b"application/json":
                    raise GatewayError(415, "invalid_content_type", "Content-Type application/json requis.")
                try:
                    length = int(headers.get(b"content-length", b"0"))
                except ValueError as exc:
                    raise GatewayError(400, "invalid_content_length", "Content-Length invalide.") from exc
                if length < 0 or length > self.settings.max_body_bytes:
                    raise GatewayError(413, "request_too_large", "Requête trop volumineuse.")
                # Bound large photo uploads before buffering or parsing them.
                # Keep the slot through generation and any streamed response.
                if self.body_requests >= self.settings.max_concurrency:
                    raise GatewayError(
                        429, "concurrency_limit", "Nombre maximal de requêtes simultanées atteint."
                    )
                self.body_requests += 1
                body_reserved = True
                body = bytearray()
                async with asyncio.timeout(self.settings.body_read_timeout):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > self.settings.max_body_bytes:
                            raise GatewayError(413, "request_too_large", "Requête trop volumineuse.")
                        if not message.get("more_body", False):
                            break
                delivered = False
                original_receive = receive

                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()

                receive = replay
            await self.app(scope, receive, wrapped_send)
        except (GatewayError, TimeoutError) as exc:
            error = safe_error(exc)
            response = JSONResponse(error.body(), status_code=error.status)
            await response(scope, receive, wrapped_send)
        finally:
            if body_reserved:
                self.body_requests -= 1
            LOG.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": scope["method"],
                        "status": status,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    }
                )
            )


def create_app(settings=None, provider_override=None):
    settings = settings or Settings()
    storage = Storage(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=10),
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        ) as client:
            app.state.provider = provider_override or (
                ChatGPTProvider(settings, client, storage)
                if settings.provider == "chatgpt_oauth"
                else OllamaProvider(settings, client)
            )
            yield

    app = FastAPI(
        title="Passerelle personnelle",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.storage = storage
    app.state.active = 0
    app.state.settings = settings
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    app.add_middleware(Guard, settings=settings, storage=storage)

    @app.exception_handler(GatewayError)
    async def handle_gateway(_request, exc):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_request, exc):
        errors = exc.errors()
        field = ".".join(str(part) for part in errors[0]["loc"] if part != "body") if errors else None
        return JSONResponse(
            GatewayError(
                400,
                "invalid_or_unsupported_parameter",
                "Champ invalide ou non pris en charge. Consultez COMPATIBILITY.md.",
                field,
            ).body(),
            status_code=400,
        )

    @app.exception_handler(HTTPException)
    async def handle_http(_request, exc):
        if exc.status_code == 400:
            invalid_encoding = isinstance(exc.__cause__, UnicodeDecodeError)
            error = GatewayError(
                400,
                "invalid_encoding" if invalid_encoding else "invalid_request_body",
                "Le corps JSON doit être encodé en UTF-8. Utilisez --data-binary @- ou un fichier JSON UTF-8."
                if invalid_encoding
                else "Corps de requête illisible. Envoyez un document JSON valide en UTF-8.",
                "body",
            )
            return JSONResponse(error.body(), status_code=400)
        return JSONResponse(
            GatewayError(
                exc.status_code,
                "unsupported_route" if exc.status_code == 404 else "invalid_request",
                "Route ou méthode non prise en charge.",
            ).body(),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def handle_unknown(_request, _exc):
        return JSONResponse(
            GatewayError(500, "internal_error", "Erreur interne. Consultez l'identifiant de requête.").body(),
            status_code=500,
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    async def available_models(request, include_alias=True, fresh=False):
        try:
            async with asyncio.timeout(min(30, settings.request_timeout)):
                get_models = (
                    getattr(app.state.provider, "readiness_models", app.state.provider.models)
                    if fresh
                    else app.state.provider.models
                )
                models = await get_models()
        except Exception as exc:
            raise safe_error(exc) from exc
        allowed = json.loads(request.state.key["models"])
        catalog = [model for model in models if not allowed or model["id"] in allowed]
        if include_alias:
            preferences = await asyncio.to_thread(storage.preferences, settings.provider)
            alias = default_alias(catalog, preferences)
            if alias:
                catalog.append(alias)
        return catalog

    @app.get("/readyz")
    async def readyz(request: Request):
        models = await available_models(request, fresh=True)
        if not models:
            raise GatewayError(503, "no_available_models", "Aucun modèle visible pour cette clé.")
        return {"status": "ready", "inference_verified": False}

    @app.get("/openapi.json")
    async def openapi():
        return app.openapi()

    @app.get("/v1/models")
    async def models(request: Request):
        return {"object": "list", "data": await available_models(request)}

    @app.get("/v1/models/{model:path}")
    async def model_detail(model: str, request: Request):
        for item in await available_models(request):
            if item["id"] == model:
                return item
        raise GatewayError(404, "model_not_found", "Modèle absent du catalogue autorisé.", "model")

    async def generate(body, request, chat=None):
        if app.state.active >= settings.max_concurrency:
            raise GatewayError(429, "concurrency_limit", "Nombre maximal de générations simultanées atteint.")
        app.state.active += 1
        iterator = None
        transferred = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.request_timeout
        try:
            async with asyncio.timeout_at(deadline):
                catalog = await available_models(request, include_alias=False)
                preferences = await asyncio.to_thread(storage.preferences, settings.provider)
                body = resolve_request(body, catalog, preferences, settings.provider)
                prepared = app.state.provider.prepare(body)
                await asyncio.to_thread(storage.reserve_request, request.state.key)
                iterator = app.state.provider.events(prepared).__aiter__()
                first = await anext(iterator)
            if body.stream:
                transferred = True
                headers = {"X-Accel-Buffering": "no", "X-Gateway-Provider": settings.provider}
                return StreamingResponse(
                    stream_events(iterator, first, body, chat, deadline),
                    media_type="text/event-stream",
                    headers=headers,
                )
            final = None
            current = asyncio.current_task()

            async def watch_disconnect():
                while True:
                    await asyncio.sleep(0.2)
                    if await request.is_disconnected():
                        current.cancel()
                        return

            watcher = asyncio.create_task(watch_disconnect())
            try:
                async with asyncio.timeout_at(deadline):
                    event = first
                    while True:
                        if event["type"] in {"response.completed", "response.incomplete"}:
                            final = event["response"]
                            break
                        event = await anext(iterator)
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            if final is None:
                raise GatewayError(502, "truncated_upstream_stream", "Réponse finale manquante.")
            result = response_to_chat(final) if chat else final
            return JSONResponse(result, headers={"X-Gateway-Provider": settings.provider})
        except Exception as exc:
            raise safe_error(exc) from exc
        finally:
            if not transferred:
                try:
                    if iterator is not None:
                        with anyio.CancelScope(shield=True):
                            await iterator.aclose()
                finally:
                    app.state.active -= 1

    async def stream_events(iterator, first, body, chat, deadline):
        chat_id = "chatcmpl-" + uuid.uuid4().hex
        first_response = first.get("response", {})
        created = int(first_response.get("created_at", time.time()))
        model = first_response.get("model", body.model)
        sequence = 0

        def chunk(delta, finish=None, usage=None, usage_only=False):
            return {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": []
                if usage_only
                else [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
                "usage": usage,
            }

        try:
            if chat:
                yield json_sse(chunk({"role": "assistant", "content": ""}))
            async with asyncio.timeout_at(deadline):
                event = first
                while True:
                    terminal = event["type"] in {"response.completed", "response.incomplete"}
                    sequence = max(sequence, event.get("sequence_number", sequence)) + 1
                    if not chat:
                        yield json_sse(event, event["type"])
                    elif event["type"] == "response.output_text.delta":
                        yield json_sse(chunk({"content": event["delta"]}))
                    elif event["type"] == "response.refusal.delta":
                        yield json_sse(chunk({"refusal": event["delta"]}))
                    if terminal:
                        if chat:
                            yield json_sse(chunk({}, chat_finish_reason(event["response"])))
                            if chat.stream_options and chat.stream_options.include_usage:
                                yield json_sse(
                                    chunk({}, usage=chat_usage(event["response"]), usage_only=True)
                                )
                            yield b"data: [DONE]\n\n"
                        return
                    event = await anext(iterator)
        except Exception as exc:
            error = safe_error(exc)
            if chat:
                yield json_sse(error.body())
                yield b"data: [DONE]\n\n"
            else:
                yield json_sse(
                    {
                        "type": "error",
                        "sequence_number": sequence,
                        "code": error.code,
                        "message": error.message,
                        "param": error.param,
                    },
                    "error",
                )
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    await iterator.aclose()
            finally:
                app.state.active -= 1

    @app.post(
        "/v1/responses", responses={200: {"content": {"application/json": {}, "text/event-stream": {}}}}
    )
    async def responses(body: ResponseRequest, request: Request):
        return await generate(body, request)

    @app.post(
        "/v1/chat/completions",
        responses={200: {"content": {"application/json": {}, "text/event-stream": {}}}},
    )
    async def completions(body: ChatRequest, request: Request):
        return await generate(chat_to_response(body), request, chat=body)

    original_openapi = app.openapi

    def secured_openapi():
        from .openapi import enrich

        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["GatewayKey"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "gw_...",
        }
        for path, methods in schema["paths"].items():
            for operation in methods.values():
                operation["security"] = [] if path == "/healthz" else [{"GatewayKey": []}]
                operation["responses"].pop("422", None)
                if path != "/healthz":
                    for code in (400, 401, 403, 404, 413, 429, 502, 503, 504):
                        operation["responses"][str(code)] = {
                            "description": "Erreur compatible : error.message/type/param/code"
                        }
        return enrich(schema)

    app.openapi = secured_openapi
    return app
