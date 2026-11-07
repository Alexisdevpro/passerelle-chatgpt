import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.contracts import ResponseRequest, response_to_chat
from gateway.errors import GatewayError, upstream_error
from gateway.providers import ChatGPTProvider, OllamaProvider, sse_events
from gateway.storage import Storage


def final_response(text="Bonjour", usage=True):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "test-model",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7} if usage else None,
    }


class ScriptedProvider:
    def __init__(self, settings):
        self.settings = settings
        self.calls = 0
        self.closed = 0
        self.failure = None
        self.no_usage = False
        self.delay = 0

    async def models(self):
        return [{"id": "test-model", "object": "model", "created": 0, "owned_by": "test"}]

    def prepare(self, request):
        return ChatGPTProvider.prepare(self, request)

    async def events(self, prepared):
        self.calls += 1
        try:
            yield {"type": "response.created", "sequence_number": 0, "response": final_response("")}
            if self.delay:
                await asyncio.sleep(self.delay)
            yield {"type": "response.output_text.delta", "sequence_number": 1, "delta": "Bonjour"}
            if self.failure:
                raise self.failure
            yield {
                "type": "response.completed",
                "sequence_number": 2,
                "response": final_response(usage=not self.no_usage),
            }
        finally:
            self.closed += 1


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, allowed_hosts=["testserver"], key_rpm=10000, ip_rpm=10000)
    provider = ScriptedProvider(settings)
    app = create_app(settings, provider)
    storage = app.state.storage
    key_id, raw = storage.issue_key("test")
    return app, provider, storage, key_id, {"Authorization": "Bearer " + raw}


@pytest.mark.parametrize("route", ["responses", "chat/completions"])
@pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
def test_accented_json_body_encoding_is_reported_correctly(setup, route, encoding):
    app, provider, _, _, headers = setup
    prompt = "Décris le rôle de cette API en trois phrases."
    payload = {"model": "test-model", "stream": True}
    payload.update(
        {"input": prompt} if route == "responses" else {"messages": [{"role": "user", "content": prompt}]}
    )
    captured = []
    prepare = provider.prepare

    def capture(request):
        result = prepare(request)
        captured.append(result)
        return result

    provider.prepare = capture
    with TestClient(app) as client:
        result = client.post(
            "/v1/" + route,
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps(payload, ensure_ascii=False).encode(encoding),
        )
    if encoding == "cp1252":
        assert result.status_code == 400
        assert result.json()["error"]["code"] == "invalid_encoding"
        assert result.json()["error"]["param"] == "body"
        assert "UTF-8" in result.json()["error"]["message"]
        assert prompt not in result.text
        assert provider.calls == 0 and not captured
    else:
        assert result.status_code == 200
        assert captured[0]["input"][0]["content"] == prompt
        assert "Bonjour" in result.text
        assert provider.calls == 1


def test_auth_and_health(setup):
    app, _, storage, key_id, headers = setup
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        for path in ["/v1/models", "/readyz", "/openapi.json", "/v1/not-implemented"]:
            result = client.get(path)
            assert result.status_code == 401
            assert result.json()["error"]["code"] == "invalid_api_key"
        assert client.get("/v1/models", headers=headers).status_code == 200
        storage.revoke_key(key_id)
        assert client.get("/v1/models", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "route,payload",
    [
        ("responses", {"input": "Salut"}),
        ("chat/completions", {"messages": [{"role": "user", "content": "Salut"}]}),
    ],
)
def test_json_and_streaming(setup, route, payload):
    app, provider, _, _, headers = setup
    with TestClient(app) as client:
        result = client.post("/v1/" + route, headers=headers, json={"model": "test-model", **payload})
        assert result.status_code == 200
        body = result.json()
        if route == "responses":
            assert body["output"][0]["content"][0]["text"] == "Bonjour"
        else:
            assert body["choices"][0]["message"]["content"] == "Bonjour"
            assert body["choices"][0]["finish_reason"] == "stop"
        result = client.post(
            "/v1/" + route, headers=headers, json={"model": "test-model", **payload, "stream": True}
        )
        assert result.status_code == 200
        assert result.headers["content-type"].startswith("text/event-stream")
        if route == "responses":
            assert "event: response.completed" in result.text
            assert "[DONE]" not in result.text
        else:
            assert '"delta":{"content":"Bonjour"}' in result.text
            assert result.text.endswith("data: [DONE]\n\n")
    assert provider.closed == 2
    assert app.state.active == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"temperature": 0.5},
        {"top_p": 1},
        {"max_output_tokens": 5},
        {"store": True},
        {"previous_response_id": "resp_old"},
        {"tools": []},
        {"metadata": {}},
        {"background": False},
        {"seed": 1},
        {"text": {"format": {"type": "json_object"}}},
        {"stream": "true"},
    ],
)
def test_reject_unsupported(setup, extra):
    app, provider, _, _, headers = setup
    with TestClient(app) as client:
        result = client.post(
            "/v1/responses", headers=headers, json={"model": "test-model", "input": "x", **extra}
        )
    assert result.status_code == 400
    assert provider.calls == 0
    assert "error" in result.json()


def test_model_scope_and_expiration(setup):
    app, _, storage, _, _ = setup
    _, raw = storage.issue_key("restricted", ["other"])
    headers = {"Authorization": "Bearer " + raw}
    with TestClient(app) as client:
        assert client.get("/v1/models", headers=headers).json()["data"] == []
        assert (
            client.post(
                "/v1/responses", headers=headers, json={"model": "test-model", "input": "x"}
            ).status_code
            == 404
        )
        _, raw = storage.issue_key("expired", expires=time.time() - 1)
        assert client.get("/v1/models", headers={"Authorization": "Bearer " + raw}).status_code == 401


def test_daily_quota_and_rate_limit(setup):
    app, _, storage, _, headers = setup
    _, raw = storage.issue_key("one", daily_limit=1)
    headers = {"Authorization": "Bearer " + raw}
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/responses", headers=headers, json={"model": "test-model", "input": "x"}
            ).status_code
            == 200
        )
        result = client.post("/v1/responses", headers=headers, json={"model": "test-model", "input": "x"})
        assert result.status_code == 429
    app.state.settings.key_rpm = 1
    _, raw = storage.issue_key("rate-test")
    headers = {"Authorization": "Bearer " + raw}
    with TestClient(app) as client:
        assert client.get("/v1/models", headers=headers).status_code == 200
        assert client.get("/v1/models", headers=headers).status_code == 429


def test_validation_security_and_openapi(setup):
    app, _, _, _, headers = setup
    with TestClient(app) as client:
        assert client.get("/healthz", headers={"Host": "attacker.test"}).status_code == 400
        assert client.get("/healthz", headers={"Origin": "https://attacker.test"}).status_code == 403
        assert client.post("/v1/responses", headers=headers, content="x").status_code == 415
        malformed = client.post(
            "/v1/responses", headers={**headers, "Content-Type": "application/json"}, content="{"
        )
        assert malformed.status_code == 400
        schema = client.get("/openapi.json", headers=headers).json()
        assert schema["components"]["schemas"]["ResponseRequest"]["additionalProperties"] is False
        assert "422" not in schema["paths"]["/v1/responses"]["post"]["responses"]
        assert schema["paths"]["/v1/models"]["get"]["security"] == [{"GatewayKey": []}]
        assert client.get("/v1/files", headers=headers).status_code == 404
        app.state.settings.max_body_bytes = 20
        assert (
            client.post(
                "/v1/responses", headers=headers, json={"model": "test-model", "input": "x"}
            ).status_code
            == 413
        )


def test_failure_after_stream_started(setup):
    app, provider, _, _, headers = setup
    provider.failure = upstream_error(
        0, {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "secret"}}
    )
    with TestClient(app) as client:
        result = client.post(
            "/v1/responses", headers=headers, json={"model": "test-model", "input": "x", "stream": True}
        )
        assert result.status_code == 200
        assert '"code":"subscription_sharing_usage_limit_exceeded"' in result.text
        assert "event: error" in result.text
        assert "response.completed" not in result.text
        assert "secret" not in result.text
        result = client.post("/v1/responses", headers=headers, json={"model": "test-model", "input": "x"})
        assert result.status_code == 429
    assert app.state.active == 0
    assert provider.closed == 2


def test_missing_usage_is_null(setup):
    app, provider, _, _, headers = setup
    provider.no_usage = True
    with TestClient(app) as client:
        result = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "test-model", "messages": [{"role": "user", "content": "x"}]},
        )
        assert result.json()["usage"] is None


async def test_deadline_and_capacity(setup):
    app, provider, _, _, headers = setup
    app.state.settings.request_timeout = 0.3
    provider.delay = 1
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            result = await client.post(
                "/v1/responses", headers=headers, json={"model": "test-model", "input": "x"}
            )
            assert result.status_code == 504
            assert provider.closed == 1
            assert app.state.active == 0
            app.state.active = 2
            result = await client.post(
                "/v1/responses", headers=headers, json={"model": "test-model", "input": "x"}
            )
            assert result.status_code == 429


@pytest.mark.parametrize("terminal_has_output", [True, False])
async def test_oauth_provider_and_partial_sse(tmp_path, terminal_has_output):
    settings = Settings(data_dir=tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "test-model", "visibility": "list"},
                        {"slug": "hidden", "visibility": "hidden"},
                    ]
                },
            )
        final = final_response()
        item = final["output"][0]
        if not terminal_has_output:
            final["output"] = []
        events = [
            {"type": "response.output_text.delta", "sequence_number": 0, "delta": "Bonjour"},
            {"type": "response.output_item.done", "sequence_number": 1, "output_index": 0, "item": item},
            {"type": "response.completed", "sequence_number": 2, "response": final},
        ]
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ChatGPTProvider(settings, client, Storage(tmp_path))

        async def token():
            return "test-credential"

        provider.session.token = token
        assert len(await provider.models()) == 1
        prepared = provider.prepare(ResponseRequest(model="test-model", input="x", stream=False))
        assert prepared["store"] is False and prepared["stream"] is True
        values = [event async for event in provider.events(prepared)]
        assert values[-1]["response"]["status"] == "completed"
        assert values[-1]["response"]["output"][0]["content"][0]["text"] == "Bonjour"
        assert json.loads(requests[-1].content)["input"] == [{"role": "user", "content": "x"}]
        response = httpx.Response(200, text='data: {"type":"response.created"}')
        with pytest.raises(GatewayError) as caught:
            _ = [event async for event in sse_events(response, 1000)]
        assert caught.value.code == "truncated_upstream_stream"


@pytest.mark.parametrize("route", ["responses", "chat/completions"])
async def test_oauth_empty_terminal_output_preserves_json_and_sse_text(tmp_path, route):
    settings = Settings(data_dir=tmp_path, allowed_hosts=["testserver"])

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"slug": "test-model", "visibility": "list"}]})
        final = final_response()
        item = final["output"][0]
        final["output"] = []
        events = [
            {"type": "response.created", "response": {**final, "status": "in_progress"}},
            {"type": "response.output_text.delta", "delta": "Bonjour"},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": final},
        ]
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        provider = ChatGPTProvider(settings, upstream, Storage(tmp_path))

        async def token():
            return "test-credential"

        provider.session.token = token
        app = create_app(settings, provider)
        _, key = app.state.storage.issue_key("test")
        headers = {"Authorization": "Bearer " + key}
        payload = {"model": "test-model"}
        payload.update(
            {"input": "Salut"}
            if route == "responses"
            else {"messages": [{"role": "user", "content": "Salut"}]}
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                result = await client.post("/v1/" + route, headers=headers, json=payload)
                assert result.status_code == 200
                if route == "responses":
                    assert result.json()["output"][0]["content"][0]["text"] == "Bonjour"
                else:
                    assert result.json()["choices"][0]["message"]["content"] == "Bonjour"
                result = await client.post("/v1/" + route, headers=headers, json={**payload, "stream": True})
                assert result.status_code == 200
                events = [
                    json.loads(line[6:])
                    for line in result.text.splitlines()
                    if line.startswith("data: ") and line != "data: [DONE]"
                ]
                if route == "responses":
                    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "Bonjour"
                else:
                    assert (
                        "".join(c["delta"].get("content", "") for e in events for c in e["choices"])
                        == "Bonjour"
                    )
                    assert result.text.endswith("data: [DONE]\n\n")


async def test_ollama_real_adapter_with_transport_fixture(tmp_path):
    settings = Settings(data_dir=tmp_path, provider="ollama")

    def handler(request):
        body = json.loads(request.content)
        assert body["options"] == {"num_predict": 5, "temperature": 0.5}
        chunks = [
            {"message": {"content": "Bon"}, "done": False},
            {
                "message": {"content": "jour"},
                "done": True,
                "done_reason": "length",
                "prompt_eval_count": 4,
                "eval_count": 2,
            },
        ]
        return httpx.Response(200, text="\n".join(json.dumps(chunk) for chunk in chunks) + "\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OllamaProvider(settings, client)
        request = ResponseRequest(model="test-model", input="x", max_output_tokens=5, temperature=0.5)
        values = [event async for event in provider.events(provider.prepare(request))]
    assert values[-1]["type"] == "response.incomplete"
    assert values[-1]["response"]["output"][0]["content"][0]["text"] == "Bonjour"
    assert values[-1]["response"]["usage"]["total_tokens"] == 6


def test_storage_contains_no_clear_credentials(tmp_path):
    store = Storage(tmp_path)
    _, key = store.issue_key("name")
    store.write_secrets({"active": "x", "accounts": {"x": {"access_token": "secret-test-value"}}})
    assert b"secret-test-value" not in (tmp_path / "accounts.enc").read_bytes()
    assert key.encode() not in (tmp_path / "gateway.sqlite3").read_bytes()
    assert store.read_secrets()["active"] == "x"
    assert store.host_id() == Storage(tmp_path).host_id()


def test_boolean_false_is_not_integer_zero():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResponseRequest(model="x", input="x", store=0)


def test_refusal_preserved_in_chat():
    response = final_response()
    response["output"][0]["content"] = [{"type": "refusal", "refusal": "Refus du modèle"}]
    chat = response_to_chat(response)
    assert chat["choices"][0]["message"]["refusal"] == "Refus du modèle"
    assert chat["choices"][0]["message"]["content"] is None


def test_content_filter_is_not_reported_as_token_limit():
    response = final_response()
    response.update(status="incomplete", incomplete_details={"reason": "content_filter"})
    assert response_to_chat(response)["choices"][0]["finish_reason"] == "content_filter"


def test_ip_limit_ignores_spoofed_forwarded_headers(setup):
    app, _, _, _, _ = setup
    app.state.settings.ip_rpm = 2
    with TestClient(app) as client:
        assert client.get("/healthz", headers={"X-Forwarded-For": "192.0.2.1"}).status_code == 200
        assert client.get("/healthz", headers={"X-Forwarded-For": "192.0.2.2"}).status_code == 200
        assert client.get("/healthz", headers={"X-Forwarded-For": "192.0.2.3"}).status_code == 429


def test_chunked_body_size_limit(setup):
    app, _, _, _, headers = setup
    app.state.settings.max_body_bytes = 20
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers={**headers, "Content-Type": "application/json"},
            content=iter([b'{"input":"', b"x" * 30, b'"}']),
        )
    assert response.status_code == 413


async def test_sse_memory_bound():
    response = httpx.Response(
        200, text='data: {"type":"response.output_text.delta","delta":"' + "x" * 1000 + '"}\n\n'
    )
    with pytest.raises(GatewayError) as caught:
        _ = [event async for event in sse_events(response, 64)]
    assert caught.value.code == "upstream_size_limit"
