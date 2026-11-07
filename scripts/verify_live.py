"""Real inference through a running gateway. No credential contents in the report."""

import argparse
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from openai import OpenAI


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8787/v1")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ollama", action="store_true")
    args = parser.parse_args()
    key = args.key_file.read_text().strip()
    client = OpenAI(base_url=args.base_url, api_key=key, max_retries=0, timeout=180)
    origin = args.base_url.removesuffix("/v1")
    results = []
    opts = {"max_output_tokens": 64} if args.ollama else {}
    chat_opts = {"max_completion_tokens": 64} if args.ollama else {}

    def record(name, action):
        start = time.monotonic()
        result = action()
        results.append(
            {"test": name, "passed": True, "seconds": round(time.monotonic() - start, 3), "observed": result}
        )
        print(name + " : OK", flush=True)

    def auth():
        response = httpx.get(args.base_url + "/models", timeout=10)
        assert response.status_code == 401
        return {"http_status": response.status_code, "code": response.json()["error"]["code"]}

    record("sans_clé", auth)

    def models():
        listed = client.models.list()
        assert args.model in {model.id for model in listed.data}
        retrieved = client.models.retrieve(args.model)
        assert retrieved.id == args.model
        return {"selected": args.model, "visible_models": len(listed.data)}

    record("catalogue_et_détail", models)

    def response_json():
        response = client.responses.create(model=args.model, input="Réponds uniquement par BONJOUR.", **opts)
        assert response.status == "completed" and "BONJOUR" in response.output_text.upper()
        return {
            "status": response.status,
            "text": response.output_text,
            "usage_available": response.usage is not None,
        }

    record("sdk_python_responses_json", response_json)

    def response_stream():
        text, completed = [], False
        with client.responses.create(
            model=args.model, input="Réponds uniquement par SALUT.", stream=True, **opts
        ) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    text.append(event.delta)
                elif event.type == "response.completed":
                    completed = True
                elif event.type in {"error", "response.failed", "response.incomplete"}:
                    raise AssertionError("Unsuccessful terminal event")
        assert completed and "SALUT" in "".join(text).upper()
        return {"completed": completed, "text": "".join(text)}

    record("sdk_python_responses_sse", response_stream)

    def chat_json():
        response = client.chat.completions.create(
            model=args.model,
            messages=[
                {"role": "system", "content": "Réponds très brièvement."},
                {"role": "user", "content": "Réponds uniquement par MERCI."},
            ],
            **chat_opts,
        )
        assert response.choices[0].finish_reason == "stop"
        assert "MERCI" in response.choices[0].message.content.upper()
        return {
            "finish_reason": response.choices[0].finish_reason,
            "text": response.choices[0].message.content,
        }

    record("sdk_python_chat_json_system", chat_json)

    def chat_stream():
        text, finish, usage = [], None, None
        with client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": "Réponds uniquement par OUI."}],
            stream=True,
            stream_options={"include_usage": True},
            **chat_opts,
        ) as stream:
            for chunk in stream:
                if chunk.usage is not None:
                    usage = chunk.usage.total_tokens
                if chunk.choices:
                    text.append(chunk.choices[0].delta.content or "")
                    finish = chunk.choices[0].finish_reason or finish
        assert finish == "stop" and "OUI" in "".join(text).upper()
        return {"finish_reason": finish, "text": "".join(text), "total_tokens": usage}

    record("sdk_python_chat_sse_usage", chat_stream)

    def multi_turn():
        response = client.responses.create(
            model=args.model,
            input=[
                {"role": "user", "content": "Mon code personnel de test est 2468."},
                {"role": "assistant", "content": "J'ai retenu le code de test."},
                {
                    "role": "user",
                    "content": "Quel est mon code de test ? Réponds uniquement par les quatre chiffres.",
                },
            ],
            **opts,
        )
        assert response.status == "completed" and "2468" in response.output_text
        return {"status": response.status, "text": response.output_text}

    record("historique_explicite", multi_turn)

    def rejection():
        headers = {"Authorization": "Bearer " + key}
        response = httpx.post(
            args.base_url + "/responses",
            headers=headers,
            json={"model": args.model, "input": "x", "previous_response_id": "resp_missing"},
            timeout=10,
        )
        assert response.status_code == 400
        return {"http_status": response.status_code, "code": response.json()["error"]["code"]}

    record("paramètre_non_supporté", rejection)

    curl = shutil.which("curl")
    if curl:

        def curl_test(stream):
            body = json.dumps(
                {
                    "model": args.model,
                    "messages": [{"role": "user", "content": "Reply exactly OK."}],
                    "stream": stream,
                    **chat_opts,
                }
            )
            # Secret passes through stdin, never through process arguments or console output.
            config = 'header = "Authorization: Bearer ' + key + '"\n'
            response = subprocess.run(
                [
                    curl,
                    "--config",
                    "-",
                    "--silent",
                    "--show-error",
                    "--fail-with-body",
                    "--no-buffer",
                    "--max-time",
                    "180",
                    args.base_url + "/chat/completions",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    body,
                ],
                input=config,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=True,
            )
            if stream:
                assert response.stdout.endswith("data: [DONE]\n\n")
                assert '"content":"OK"' in response.stdout or '"content":"O' in response.stdout
                return {"terminal_done": True}
            value = json.loads(response.stdout)
            assert value["choices"][0]["finish_reason"] == "stop"
            return {"finish_reason": "stop", "text": value["choices"][0]["message"]["content"]}

        record("curl_chat_json", lambda: curl_test(False))
        record("curl_chat_sse", lambda: curl_test(True))
    report = {
        "at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "backend": "ollama" if args.ollama else "chatgpt_oauth",
        "model": args.model,
        "health": httpx.get(origin + "/healthz").json(),
        "tests": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Rapport : " + str(args.report))


if __name__ == "__main__":
    main()
