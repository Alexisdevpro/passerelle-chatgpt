import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_gateway import ScriptedProvider

from gateway.app import create_app
from gateway.config import Settings
from gateway.contracts import ChatRequest, ResponseRequest, chat_to_response
from gateway.errors import GatewayError
from gateway.providers import ChatGPTProvider, OllamaProvider

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF1sAAAAASUVORK5CYII="
URL = "https://images.example.org/photo.jpg?signature=private-test-value"


def image_part(route, url=PNG, detail="high"):
    if route == "responses":
        return {"type": "input_image", "image_url": url, "detail": detail}
    return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


def payload(route, parts, role="user"):
    message = {"role": role, "content": parts}
    return {"model": "test-model", "input" if route == "responses" else "messages": [message]}


class VisionProvider(ScriptedProvider):
    def __init__(self, settings):
        super().__init__(settings)
        self.prepared = []

    def prepare(self, request):
        result = ChatGPTProvider.prepare(self, request)
        self.prepared.append(result)
        return result


@pytest.fixture
def vision(tmp_path):
    settings = Settings(data_dir=tmp_path, allowed_hosts=["testserver"], key_rpm=1000, ip_rpm=1000)
    provider = VisionProvider(settings)
    app = create_app(settings, provider)
    _, key = app.state.storage.issue_key("vision-test")
    with TestClient(app) as client:
        yield client, provider, {"Authorization": "Bearer " + key}


@pytest.mark.parametrize("route", ["responses", "chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
def test_photo_and_text_preserved_in_order_for_json_and_streaming(vision, route, stream):
    client, provider, headers = vision
    text_type = "input_text" if route == "responses" else "text"
    parts = [
        {"type": text_type, "text": "Compare :"},
        image_part(route),
        {"type": text_type, "text": "avec celle-ci"},
        image_part(route, URL, "low"),
    ]
    result = client.post("/v1/" + route, headers=headers, json={**payload(route, parts), "stream": stream})
    assert result.status_code == 200
    assert "Bonjour" in result.text
    if stream:
        assert (
            "response.completed" in result.text
            if route == "responses"
            else result.text.endswith("data: [DONE]\n\n")
        )
    prepared = provider.prepared[-1]
    assert prepared["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Compare :"},
                {"type": "input_image", "image_url": PNG, "detail": "high"},
                {"type": "input_text", "text": "avec celle-ci"},
                {"type": "input_image", "image_url": URL, "detail": "low"},
            ],
        }
    ]
    assert prepared["store"] is False and prepared["stream"] is True


@pytest.mark.parametrize("route", ["responses", "chat/completions"])
def test_image_only_and_large_inline_image_are_accepted(vision, route):
    client, provider, headers = vision
    # The provider validates complete images; this fixture exercises transport
    # limits and the local MIME/signature check without an image decoder.
    image = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 900_000).decode()
    result = client.post("/v1/" + route, headers=headers, json=payload(route, [image_part(route, image)]))
    assert result.status_code == 200
    assert int(result.request.headers["content-length"]) > 1_048_576
    assert provider.prepared[-1]["input"][0]["content"][0]["image_url"] == image


@pytest.mark.parametrize("role", ["system", "developer", "assistant"])
@pytest.mark.parametrize("route", ["responses", "chat/completions"])
def test_images_outside_user_messages_rejected_before_upstream(vision, route, role):
    client, provider, headers = vision
    result = client.post("/v1/" + route, headers=headers, json=payload(route, [image_part(route)], role))
    assert result.status_code == 400
    assert provider.calls == 0 and not provider.prepared


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "C:/photo.jpg",
        "http://images.example.org/photo.jpg",
        "https://user:password@images.example.org/photo.jpg",
        "https://images.example.org:bad/photo.jpg",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64,not-valid!",
        "data:image/jpeg;base64,iVBORw0KGgo=",
        "data:image/png;base64,",
    ],
)
def test_invalid_image_references_rejected_without_echoing_payload(vision, url):
    client, provider, headers = vision
    result = client.post(
        "/v1/responses", headers=headers, json=payload("responses", [image_part("responses", url)])
    )
    assert result.status_code == 400
    assert url not in result.text and "password" not in result.text
    assert provider.calls == 0


@pytest.mark.parametrize("route", ["responses", "chat/completions"])
def test_image_count_includes_all_messages(vision, route):
    client, provider, headers = vision
    body = payload(route, [image_part(route)] * 4)
    messages = body["input" if route == "responses" else "messages"]
    messages.append({"role": "user", "content": [image_part(route)] * 5})
    result = client.post("/v1/" + route, headers=headers, json=body)
    assert result.status_code == 400 and result.json()["error"]["code"] == "too_many_images"
    assert provider.calls == 0


def test_per_image_size_limit_before_upstream(vision, monkeypatch):
    import gateway.images

    monkeypatch.setattr(gateway.images, "MAX_IMAGE_BYTES", 8)
    monkeypatch.setattr(gateway.images, "MAX_BASE64_CHARS", 12)
    client, provider, headers = vision
    result = client.post(
        "/v1/responses", headers=headers, json=payload("responses", [image_part("responses")])
    )
    assert result.status_code == 413 and result.json()["error"]["code"] == "image_too_large"
    assert provider.calls == 0


def test_file_ids_and_invalid_detail_are_rejected(vision):
    client, provider, headers = vision
    for part in [
        {"type": "input_image", "file_id": "file-test"},
        image_part("responses", detail="ultra"),
        {"type": "input_file", "file_data": "data:application/pdf;base64,dGVzdA=="},
    ]:
        result = client.post("/v1/responses", headers=headers, json=payload("responses", [part]))
        assert result.status_code == 400
    assert provider.calls == 0


def test_text_history_instructions_and_selection_survive_photo_translation():
    chat = ChatRequest.model_validate(
        {
            "model": "test-model",
            "reasoning_effort": "none",
            "messages": [
                {"role": "system", "content": "Sois concis."},
                {"role": "assistant", "content": "Envoie une photo."},
                {"role": "user", "content": [image_part("chat/completions")]},
            ],
        }
    )
    response = chat_to_response(chat)
    prepared = ChatGPTProvider.prepare(None, response)
    assert prepared["instructions"] == "Sois concis."
    assert prepared["input"][0] == {"role": "assistant", "content": "Envoie une photo."}
    assert prepared["input"][1]["content"][0]["image_url"] == PNG
    assert prepared["reasoning"] == {"effort": "none"}
    with pytest.raises(GatewayError) as error:
        OllamaProvider.prepare(
            None, ResponseRequest.model_validate(payload("responses", [image_part("responses")]))
        )
    assert error.value.code == "unsupported_parameter"


def test_openapi_publishes_both_image_input_formats(vision):
    client, _, headers = vision
    schemas = client.get("/openapi.json", headers=headers).json()["components"]["schemas"]
    assert "image_url" in schemas["InputImage"]["properties"]
    assert "url" in schemas["ChatImageURL"]["properties"]
    assert "original" in schemas["InputImage"]["properties"]["detail"]["enum"]


@pytest.mark.parametrize("cancel_upload", [False, True])
async def test_uploads_share_generation_capacity_and_release_it(tmp_path, cancel_upload):
    settings = Settings(data_dir=tmp_path, allowed_hosts=["testserver"], max_concurrency=1)
    provider = VisionProvider(settings)
    app = create_app(settings, provider)
    _, key = app.state.storage.issue_key("upload-test")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    started, release = asyncio.Event(), asyncio.Event()
    body = json.dumps(payload("responses", [image_part("responses")])).encode()

    async def slow_upload():
        yield body[:20]
        started.set()
        await release.wait()
        yield body[20:]

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            pending = asyncio.create_task(
                client.post("/v1/responses", headers=headers, content=slow_upload())
            )
            try:
                await asyncio.wait_for(started.wait(), 3)
                rejected = await client.post(
                    "/v1/responses", headers=headers, json=payload("responses", [image_part("responses")])
                )
                assert rejected.status_code == 429
                assert rejected.json()["error"]["code"] == "concurrency_limit"
                assert provider.calls == 0
                if cancel_upload:
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    release.set()
                    assert (await pending).status_code == 200
                accepted = await client.post(
                    "/v1/responses", headers=headers, json=payload("responses", [image_part("responses")])
                )
                assert accepted.status_code == 200
            finally:
                release.set()
                if not pending.done():
                    await pending
