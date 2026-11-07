import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_gateway import ScriptedProvider

from gateway.app import create_app
from gateway.config import Settings
from gateway.contracts import ResponseRequest
from gateway.errors import GatewayError
from gateway.providers import ChatGPTProvider
from gateway.selection import DEFAULT_ALIAS, resolve_request, validate_selection
from gateway.storage import Storage


def catalog():
    return [
        {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "test",
            "x_gateway": {"reasoning_efforts": efforts, "reasoning_efforts_known": True},
        }
        for name, efforts in [("model-a", ["low", "medium"]), ("model-b", ["high", "max"])]
    ]


class RecordingProvider(ScriptedProvider):
    def __init__(self, settings):
        super().__init__(settings)
        self.prepared = []

    async def models(self):
        return catalog()

    async def events(self, prepared):
        self.prepared.append(prepared)
        async for event in super().events(prepared):
            if "response" in event:
                event["response"]["model"] = prepared["model"]
            yield event


def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, allowed_hosts=["testserver"], key_rpm=1000, ip_rpm=1000)
    provider = RecordingProvider(settings)
    app = create_app(settings, provider)
    storage = app.state.storage
    _, key = storage.issue_key("owner")
    return app, provider, storage, {"Authorization": "Bearer " + key}


def test_alias_changes_without_restart_and_explicit_fields_win(tmp_path):
    app, provider, storage, headers = setup(tmp_path)
    storage.set_preferences("chatgpt_oauth", "model-a", "low")
    with TestClient(app) as client:
        models = client.get("/v1/models", headers=headers).json()["data"]
        alias = next(model for model in models if model["id"] == DEFAULT_ALIAS)
        assert alias["x_gateway"]["target_model"] == "model-a"
        response = client.post("/v1/responses", headers=headers, json={"model": DEFAULT_ALIAS, "input": "x"})
        assert response.status_code == 200 and response.json()["model"] == "model-a"
        assert provider.prepared[-1]["reasoning"] == {"effort": "low"}
        storage.set_preferences("chatgpt_oauth", "model-b", "high")
        response = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": DEFAULT_ALIAS,
                "messages": [{"role": "user", "content": "x"}],
                "reasoning_effort": "max",
            },
        )
        assert response.status_code == 200 and response.json()["model"] == "model-b"
        assert provider.prepared[-1]["reasoning"] == {"effort": "max"}
        response = client.post("/v1/responses", headers=headers, json={"model": "model-a", "input": "x"})
        assert response.status_code == 200
        assert provider.prepared[-1]["model"] == "model-a"
        assert "reasoning" not in provider.prepared[-1]


def test_alias_cannot_bypass_key_model_permissions(tmp_path):
    app, provider, storage, _ = setup(tmp_path)
    storage.set_preferences("chatgpt_oauth", "model-b", "high")
    _, raw = storage.issue_key("restricted", ["model-a"])
    headers = {"Authorization": "Bearer " + raw}
    with TestClient(app) as client:
        assert DEFAULT_ALIAS not in {
            model["id"] for model in client.get("/v1/models", headers=headers).json()["data"]
        }
        response = client.post("/v1/responses", headers=headers, json={"model": DEFAULT_ALIAS, "input": "x"})
        assert response.status_code == 404
        assert provider.calls == 0


@pytest.mark.parametrize(
    "route,payload",
    [
        ("responses", {"input": "x", "reasoning": {"effort": "max"}}),
        ("chat/completions", {"messages": [{"role": "user", "content": "x"}], "reasoning_effort": "max"}),
    ],
)
def test_max_effort_translated_on_both_routes(tmp_path, route, payload):
    app, provider, _, headers = setup(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/" + route, headers=headers, json={"model": "model-b", **payload})
    assert response.status_code == 200
    assert provider.prepared[-1]["reasoning"] == {"effort": "max"}


def test_invalid_effort_is_not_silently_downgraded(tmp_path):
    app, provider, _, headers = setup(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            headers=headers,
            json={"model": "model-a", "input": "x", "reasoning": {"effort": "max"}},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_reasoning_effort"
        assert provider.calls == 0


def test_unconfigured_alias_and_provider_preferences(tmp_path):
    app, _, storage, headers = setup(tmp_path)
    with TestClient(app) as client:
        response = client.post("/v1/responses", headers=headers, json={"model": DEFAULT_ALIAS, "input": "x"})
        assert (
            response.status_code == 400 and response.json()["error"]["code"] == "default_model_not_configured"
        )
    storage.set_preferences("chatgpt_oauth", "model-a", "medium")
    storage.set_preferences("ollama", "local-model", None)
    assert storage.preferences("chatgpt_oauth") == {"model": "model-a", "effort": "medium"}
    assert storage.preferences("ollama") == {"model": "local-model", "effort": None}
    assert Storage(tmp_path).preferences("chatgpt_oauth")["effort"] == "medium"


def test_local_backend_does_not_ignore_saved_reasoning():
    with pytest.raises(GatewayError) as error:
        validate_selection(catalog(), "model-a", "low", "ollama")
    assert error.value.code == "unsupported_reasoning_effort"


def test_alias_effort_can_be_overridden_per_request():
    request = ResponseRequest(model=DEFAULT_ALIAS, input="x", reasoning={"effort": "medium"})
    result = resolve_request(request, catalog(), {"model": "model-a", "effort": "low"}, "chatgpt_oauth")
    assert result.model == "model-a" and result.reasoning.effort == "medium"
    assert request.model == DEFAULT_ALIAS


async def test_catalog_exposes_only_sanitized_effort_metadata(tmp_path):
    def handler(_):
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "slug": "model-b",
                        "visibility": "list",
                        "display_name": "Modèle B",
                        "default_reasoning_level": "low",
                        "base_instructions": "do-not-export",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "max"},
                            {"effort": "ultra"},
                        ],
                    }
                ]
            },
        )

    settings = Settings(data_dir=tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ChatGPTProvider(settings, client, Storage(tmp_path))

        async def token():
            return "test-credential"

        provider.session.token = token
        models = await provider.models()
    assert models[0]["x_gateway"]["reasoning_efforts"] == ["low", "max"]
    assert "do-not-export" not in json.dumps(models)
    assert "ultra" not in json.dumps(models)
