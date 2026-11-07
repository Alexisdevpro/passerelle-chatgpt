import socket
import threading
import time

import httpx
import pytest
import uvicorn
from test_gateway import ScriptedProvider

from gateway.app import create_app
from gateway.config import Settings


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Condition not reached within the deadline")


def test_real_socket_disconnect_closes_provider_and_releases_capacity(tmp_path):
    settings = Settings(data_dir=tmp_path, max_concurrency=1)
    provider = ScriptedProvider(settings)
    provider.delay = 10
    app = create_app(settings, provider)
    _, raw = app.state.storage.issue_key("socket-test")
    headers = {"Authorization": "Bearer " + raw}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="critical")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    wait_until(lambda: server.started)
    url = f"http://127.0.0.1:{port}/v1/responses"
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            with client.stream(
                "POST", url, headers=headers, json={"model": "test-model", "input": "x", "stream": True}
            ) as response:
                assert response.status_code == 200
                assert next(response.iter_lines()) == "event: response.created"
                # Reading a created event proves the entire output was not buffered.
            wait_until(lambda: provider.closed == 1 and app.state.active == 0)
            with pytest.raises(httpx.ReadTimeout):
                client.post(url, headers=headers, json={"model": "test-model", "input": "x"}, timeout=0.1)
            wait_until(lambda: provider.closed == 2 and app.state.active == 0)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()
