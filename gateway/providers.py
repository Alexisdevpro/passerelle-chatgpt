import asyncio
import copy
import hashlib
import json
import time
import uuid
from abc import ABC, abstractmethod

from .contracts import REASONING_EFFORTS, normalized_input
from .errors import GatewayError, upstream_error
from .oauth import RESOURCE, OAuthSession


async def bounded_lines(response, limit):
    buffer = b""
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > limit:
            raise GatewayError(502, "upstream_size_limit", "La réponse dépasse la limite mémoire configurée.")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.rstrip(b"\r").decode("utf-8")
    if buffer:
        yield buffer.rstrip(b"\r").decode("utf-8")


async def sse_events(response, limit):
    data = []
    async for line in bounded_lines(response, limit):
        if not line:
            if data:
                value = "\n".join(data)
                data = []
                if value == "[DONE]":
                    return
                event = json.loads(value)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise GatewayError(502, "invalid_upstream_stream", "Événement SSE invalide.")
                yield event
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        raise GatewayError(502, "truncated_upstream_stream", "Événement SSE final tronqué.")


async def check_http(response, limit=65536):
    if response.status_code < 400:
        return
    body = b""
    async for chunk in response.aiter_bytes(chunk_size=4096):
        body += chunk
        if len(body) > limit:
            break
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        value = None
    raise upstream_error(response.status_code, value)


def check_terminal(event):
    event_type = event.get("type")
    if event_type in {"error", "response.failed"}:
        error = (
            event.get("response", {}).get("error")
            if event_type == "response.failed"
            else (event.get("error") if isinstance(event.get("error"), dict) else event)
        )
        raise upstream_error(0, {"error": error})
    if event_type in {"response.completed", "response.incomplete"}:
        response = event.get("response")
        if (
            not isinstance(response, dict)
            or response.get("object") != "response"
            or not isinstance(response.get("output"), list)
        ):
            raise GatewayError(502, "invalid_upstream_response", "Objet final Responses invalide.")
        if not all(field in response for field in ("id", "model", "created_at", "status")):
            raise GatewayError(502, "invalid_upstream_response", "Objet final Responses incomplet.")
        # Output remains text/reasoning only; image inputs do not enable tools.
        for item in response["output"]:
            if item.get("type") not in {"message", "reasoning"}:
                raise GatewayError(
                    502,
                    "unsupported_upstream_output",
                    "Type de sortie non pris en charge par cet adaptateur.",
                )
        expected = "completed" if event_type == "response.completed" else "incomplete"
        if response["status"] != expected:
            raise GatewayError(502, "invalid_upstream_response", "Statut terminal incohérent.")
        return True
    return False


class Provider(ABC):
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    @abstractmethod
    async def models(self): ...

    async def readiness_models(self):
        return await self.models()

    @abstractmethod
    def prepare(self, request): ...

    @abstractmethod
    async def events(self, prepared):
        yield {}


class ChatGPTProvider(Provider):
    def __init__(self, settings, client, storage):
        super().__init__(settings, client)
        self.session = OAuthSession(storage)
        self._models_lock = asyncio.Lock()
        self._models_cache = None

    async def models(self, force=False):
        token = await self.session.token()
        identity = hashlib.sha256(token.encode()).digest()
        async with self._models_lock:
            if self._models_cache and not force:
                previous_identity, fetched, models = self._models_cache
                if (
                    identity == previous_identity
                    and time.monotonic() - fetched < self.settings.catalog_ttl_seconds
                ):
                    return copy.deepcopy(models)
            models = await self._fetch_models(token)
            self._models_cache = (identity, time.monotonic(), models)
            return copy.deepcopy(models)

    async def readiness_models(self):
        return await self.models(force=True)

    async def _fetch_models(self, token):
        async with self.client.stream(
            "GET", RESOURCE + "/models", headers={"Authorization": "Bearer " + token}
        ) as response:
            await check_http(response)
            raw = b"".join(
                [line.encode() async for line in bounded_lines(response, self.settings.max_output_bytes)]
            )
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("models"), list):
            raise GatewayError(502, "invalid_model_catalog", "Catalogue OAuth inattendu.")
        models = []
        for model in body["models"]:
            if model.get("visibility") != "list" or not isinstance(model.get("slug"), str):
                continue
            levels = model.get("supported_reasoning_levels")
            efforts = (
                list(
                    dict.fromkeys(
                        entry["effort"]
                        for entry in levels
                        if isinstance(entry, dict) and entry.get("effort") in REASONING_EFFORTS
                    )
                )
                if isinstance(levels, list)
                else []
            )
            display = model.get("display_name")
            # Public model documentation supports none, although the OAuth UI
            # catalog omits it. Verified with real ChatGPT-plan inference.
            instant_verified = model["slug"] == "gpt-6-luna"
            if instant_verified and "none" not in efforts:
                efforts.insert(0, "none")
            metadata = {
                "display_name": display[:200] if isinstance(display, str) else model["slug"],
                "reasoning_efforts": efforts,
                "reasoning_efforts_known": isinstance(levels, list),
                "instant_supported": instant_verified or "none" in efforts,
                "default_reasoning_effort": model.get("default_reasoning_level")
                if model.get("default_reasoning_level") in REASONING_EFFORTS
                else None,
            }
            models.append(
                {
                    "id": model["slug"],
                    "object": "model",
                    "created": 0,
                    "owned_by": "openai",
                    "x_gateway": metadata,
                }
            )
        return models

    def prepare(self, request):
        for field in ("temperature", "top_p", "max_output_tokens"):
            if getattr(request, field) is not None:
                raise GatewayError(
                    400,
                    "unsupported_parameter",
                    f"{field} n'est pas accepté par le flux ChatGPT OAuth.",
                    field,
                )
        messages, instructions = normalized_input(request)
        result = {"model": request.model, "input": messages, "stream": True, "store": False}
        if instructions is not None:
            result["instructions"] = instructions
        if request.reasoning is not None:
            result["reasoning"] = request.reasoning.model_dump()
        return result

    async def events(self, prepared):
        token = await self.session.token()
        terminal = False
        completed_output = {}
        async with self.client.stream(
            "POST",
            RESOURCE + "/responses",
            json=prepared,
            headers={"Authorization": "Bearer " + token, "Accept": "text/event-stream"},
        ) as response:
            await check_http(response)
            content_type = response.headers.get("content-type", "")
            if content_type and "text/event-stream" not in content_type:
                raise GatewayError(502, "invalid_upstream_stream", "Le backend n'a pas renvoyé de SSE.")
            async for event in sse_events(response, self.settings.max_output_bytes):
                if event["type"] == "response.output_item.done":
                    index, item = event.get("output_index"), event.get("item")
                    if type(index) is not int or index < 0 or not isinstance(item, dict):
                        raise GatewayError(502, "invalid_upstream_response", "Élément de sortie invalide.")
                    completed_output[index] = item
                if (
                    event["type"] in {"response.completed", "response.incomplete"}
                    and isinstance(event.get("response"), dict)
                    and event["response"].get("output") == []
                    and completed_output
                ):
                    # Some store:false streams deliver full items only in
                    # output_item.done and leave the terminal output empty.
                    # Restore those completed items for both JSON and SSE.
                    event = {
                        **event,
                        "response": {
                            **event["response"],
                            "output": [completed_output[index] for index in sorted(completed_output)],
                        },
                    }
                terminal = check_terminal(event)
                yield event
                if terminal:
                    return
        if not terminal:
            raise GatewayError(502, "truncated_upstream_stream", "Flux interrompu sans réponse terminale.")


class OllamaProvider(Provider):
    async def models(self):
        async with self.client.stream("GET", self.settings.ollama_url + "/api/tags") as response:
            await check_http(response)
            raw = b"".join(
                [line.encode() async for line in bounded_lines(response, self.settings.max_output_bytes)]
            )
        body = json.loads(raw)
        return [
            {"id": model["name"], "object": "model", "created": 0, "owned_by": "local-ollama"}
            for model in body["models"]
        ]

    def prepare(self, request):
        if request.reasoning is not None:
            raise GatewayError(
                400,
                "unsupported_parameter",
                "reasoning n'a pas d'équivalence garantie dans ce backend local.",
                "reasoning",
            )
        messages, instructions = normalized_input(request)
        if instructions is not None:
            messages.insert(0, {"role": "system", "content": instructions})
        if any(not isinstance(item["content"], str) for item in messages):
            raise GatewayError(
                400,
                "unsupported_parameter",
                "Les images sont prises en charge par le backend ChatGPT uniquement.",
                "input",
            )
        if any(item["role"] == "developer" for item in messages):
            raise GatewayError(
                400,
                "unsupported_parameter",
                "Utilisez system : Ollama ne garantit pas le rôle developer.",
                "input",
            )
        options = {}
        for field, target in (
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("max_output_tokens", "num_predict"),
        ):
            if getattr(request, field) is not None:
                options[target] = getattr(request, field)
        return {"model": request.model, "messages": messages, "stream": True, "options": options}

    async def events(self, prepared):
        response_id, message_id = "resp_" + uuid.uuid4().hex, "msg_" + uuid.uuid4().hex
        created, sequence = int(time.time()), 0
        text_parts = []
        base = {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "in_progress",
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "model": prepared["model"],
            "output": [],
            "parallel_tool_calls": False,
            "tool_choice": "none",
            "tools": [],
            "store": False,
            "usage": None,
            "metadata": {},
            "text": {"format": {"type": "text"}},
        }

        def event(kind, **kwargs):
            nonlocal sequence
            result = {"type": kind, "sequence_number": sequence, **kwargs}
            sequence += 1
            return result

        async with self.client.stream(
            "POST", self.settings.ollama_url + "/api/chat", json=prepared
        ) as response:
            await check_http(response)
            # Wait for actual upstream admission before emitting created.
            yield event("response.created", response={**base})
            yield event("response.in_progress", response={**base})
            item = {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
            yield event("response.output_item.added", output_index=0, item=item)
            yield event(
                "response.content_part.added",
                output_index=0,
                content_index=0,
                item_id=message_id,
                part={"type": "output_text", "text": "", "annotations": [], "logprobs": []},
            )
            async for line in bounded_lines(response, self.settings.max_output_bytes):
                if not line:
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise GatewayError(502, "ollama_generation_failed", "La génération Ollama a échoué.")
                if chunk.get("message", {}).get("tool_calls"):
                    raise GatewayError(
                        502, "unsupported_upstream_output", "Appel d'outil inattendu du moteur local."
                    )
                text = chunk.get("message", {}).get("content", "")
                if text:
                    text_parts.append(text)
                    yield event(
                        "response.output_text.delta",
                        item_id=message_id,
                        output_index=0,
                        content_index=0,
                        delta=text,
                        logprobs=[],
                    )
                if not chunk.get("done"):
                    continue
                full_text = "".join(text_parts)
                yield event(
                    "response.output_text.done",
                    item_id=message_id,
                    output_index=0,
                    content_index=0,
                    text=full_text,
                    logprobs=[],
                )
                part = {"type": "output_text", "text": full_text, "annotations": [], "logprobs": []}
                yield event(
                    "response.content_part.done",
                    item_id=message_id,
                    output_index=0,
                    content_index=0,
                    part=part,
                )
                incomplete = chunk.get("done_reason") == "length"
                final_item = {
                    **item,
                    "status": "incomplete" if incomplete else "completed",
                    "content": [part],
                }
                yield event("response.output_item.done", output_index=0, item=final_item)
                usage = None
                if type(chunk.get("prompt_eval_count")) is int and type(chunk.get("eval_count")) is int:
                    usage = {
                        "input_tokens": chunk["prompt_eval_count"],
                        "output_tokens": chunk["eval_count"],
                        "total_tokens": chunk["prompt_eval_count"] + chunk["eval_count"],
                    }
                final = {
                    **base,
                    "status": "incomplete" if incomplete else "completed",
                    "output": [final_item],
                    "usage": usage,
                    "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
                }
                yield event("response.incomplete" if incomplete else "response.completed", response=final)
                return
        raise GatewayError(
            502, "truncated_upstream_stream", "Ollama a interrompu le flux sans événement final."
        )
