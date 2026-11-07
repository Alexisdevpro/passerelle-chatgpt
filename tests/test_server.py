import binascii
import json
import time

import httpx
import pytest

from gateway.config import Settings
from gateway.errors import GatewayError
from gateway.migration import export_state, import_state
from gateway.network import client_ip
from gateway.providers import ChatGPTProvider, check_terminal
from gateway.selection import validate_selection
from gateway.storage import Storage


def connected_storage(path):
    store = Storage(path)
    store.initialize()
    store.set_preferences("chatgpt_oauth", "gpt-6-luna", "none")
    store.write_secrets(
        {
            "active": "registered-test",
            "accounts": {
                "registered-test": {
                    "client_id": "registered-test",
                    "access_token": "access-test",
                    "refresh_token": "refresh-test",
                    "subject": "subject-test",
                    "scopes": ["chatgpt.tokens.use.direct", "resource.invoke"],
                    "expires_at": time.time() + 3600,
                }
            },
        }
    )
    return store


def test_private_migration_preserves_server_identity_and_existing_client_key(tmp_path):
    source = connected_storage(tmp_path / "pc")
    target = Storage(tmp_path / "server")
    target.initialize()
    server_host = target.host_id()
    key = (source.root / "client-key.txt").read_text().strip()
    import_state(target, export_state(source))
    assert target.host_id() == server_host and target.host_id() != source.host_id()
    assert target.authenticate(key)["name"] == "local-owner"
    assert target.preferences("chatgpt_oauth") == {"model": "gpt-6-luna", "effort": "none"}
    assert target.read_secrets()["active"] == "registered-test"


def test_migration_refuses_overwriting_an_existing_session(tmp_path):
    source = connected_storage(tmp_path / "pc")
    target = connected_storage(tmp_path / "server")
    previous = (target.root / "master.key").read_bytes()
    with pytest.raises(GatewayError) as error:
        import_state(target, export_state(source))
    assert error.value.status == 409
    assert (target.root / "master.key").read_bytes() == previous


def test_invalid_migration_does_not_modify_target(tmp_path):
    source = connected_storage(tmp_path / "pc")
    target = Storage(tmp_path / "server")
    target.initialize()
    previous = (target.root / "master.key").read_bytes()
    payload = json.loads(export_state(source))
    payload["accounts"] = "broken-data"
    with pytest.raises(binascii.Error):
        import_state(target, json.dumps(payload))
    assert (target.root / "master.key").read_bytes() == previous
    assert target.read_secrets()["accounts"] == {}


def test_migration_refuses_unknown_fields_and_missing_source_session(tmp_path):
    target = Storage(tmp_path)
    target.initialize()
    with pytest.raises(GatewayError):
        export_state(target)
    with pytest.raises(ValueError):
        import_state(target, '{"version":1,"host-id":"do-not-import"}')


def test_forwarded_ip_only_from_exact_trusted_proxy():
    trusted = ["172.30.87.3/32"]
    assert client_ip("192.168.1.10", "1.2.3.4", trusted) == "192.168.1.10"
    assert client_ip("172.30.87.3", "198.51.100.4", trusted) == "198.51.100.4"
    assert client_ip("172.30.87.3", "1.2.3.4, 198.51.100.4", trusted) == "198.51.100.4"
    with pytest.raises(GatewayError):
        client_ip("172.30.87.3", "not-an-ip", trusted)


def test_proxy_config_never_trusts_the_entire_internet():
    with pytest.raises(ValueError):
        Settings(trusted_proxy_cidrs=["0.0.0.0/0"])


def test_real_nested_quota_error_returns_429_instead_of_502():
    event = {
        "type": "error",
        "sequence_number": 0,
        "error": {
            "type": "insufficient_quota",
            "code": "subscription_sharing_usage_limit_exceeded",
            "param": None,
            "message": "upstream diagnostic must not leak",
        },
    }
    with pytest.raises(GatewayError) as error:
        check_terminal(event)
    assert error.value.status == 429
    assert error.value.code == "subscription_sharing_usage_limit_exceeded"
    assert "diagnostic" not in error.value.message


async def test_instant_profile_and_account_bound_catalog_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "slug": "gpt-6-luna",
                        "visibility": "list",
                        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}],
                    }
                ]
            },
        )

    settings = Settings(data_dir=tmp_path)
    credential = ["test-account-a"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ChatGPTProvider(settings, client, Storage(tmp_path))

        async def token():
            return credential[0]

        provider.session.token = token
        models = await provider.models()
        assert models[0]["x_gateway"]["reasoning_efforts"] == ["none", "low", "max"]
        validate_selection(models, "gpt-6-luna", "none", "chatgpt_oauth")
        models[0]["id"] = "mutated-copy"
        assert (await provider.models())[0]["id"] == "gpt-6-luna"
        assert len(calls) == 1
        await provider.readiness_models()
        assert len(calls) == 2
        credential[0] = "test-account-b"
        await provider.models()
        assert len(calls) == 3
