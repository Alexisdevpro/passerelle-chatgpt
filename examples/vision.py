"""Send local photos to the gateway and receive text (pip install openai)."""

import argparse
import base64
import json
import os
from pathlib import Path

MIMES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def main():
    parser = argparse.ArgumentParser(description="Envoyer des photos à la gateway et recevoir du texte.")
    parser.add_argument("photos", nargs="+", type=Path)
    parser.add_argument("--prompt", default="Décris ces photos en français.")
    parser.add_argument("--api", choices=["chat", "responses"], default="chat")
    parser.add_argument("--model", default="passerelle-default")
    parser.add_argument("--effort", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--detail", choices=["auto", "low", "high", "original"], default="auto")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument(
        "--base-url", default=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8787/v1")
    )
    parser.add_argument(
        "--save-request", type=Path, help="Écrire le JSON pour curl, sans envoyer la requête."
    )
    args = parser.parse_args()
    if len(args.photos) > 8:
        parser.error("Huit photos maximum.")
    parts = [{"type": "text" if args.api == "chat" else "input_text", "text": args.prompt}]
    for path in args.photos:
        mime = MIMES.get(path.suffix.lower())
        if not mime:
            parser.error("Formats acceptés : JPEG, PNG, WEBP et GIF non animé.")
        if not path.is_file():
            parser.error("Photo introuvable : " + str(path))
        if path.stat().st_size > 10 * 1024 * 1024:
            parser.error("Chaque photo est limitée à 10 Mio avant base64.")
        url = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
        parts.append(
            {"type": "image_url", "image_url": {"url": url, "detail": args.detail}}
            if args.api == "chat"
            else {"type": "input_image", "image_url": url, "detail": args.detail}
        )
    payload = {"model": args.model, "stream": args.stream}
    payload["messages" if args.api == "chat" else "input"] = [{"role": "user", "content": parts}]
    if args.effort:
        payload["reasoning_effort" if args.api == "chat" else "reasoning"] = (
            args.effort if args.api == "chat" else {"effort": args.effort}
        )
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode("utf-8")) + 1 > 16 * 1024 * 1024:
        parser.error("Le JSON complet dépasse 16 Mio. Réduire les photos ou en envoyer moins.")
    if args.save_request:
        args.save_request.write_text(encoded + "\n", encoding="utf-8")
        print("Requête écrite dans " + str(args.save_request))
        return
    if not os.environ.get("GATEWAY_API_KEY"):
        parser.error("Définir GATEWAY_API_KEY avec la clé personnelle de gateway.")
    from openai import APIError, OpenAI

    try:
        with OpenAI(
            base_url=args.base_url, api_key=os.environ["GATEWAY_API_KEY"], max_retries=0, timeout=190
        ) as client:
            if args.api == "chat":
                result = client.chat.completions.create(**payload)
                if args.stream:
                    with result:
                        for chunk in result:
                            if chunk.choices and chunk.choices[0].delta.content:
                                print(chunk.choices[0].delta.content, end="", flush=True)
                    print()
                else:
                    print(result.choices[0].message.content)
            else:
                result = client.responses.create(**payload)
                if args.stream:
                    with result:
                        for event in result:
                            if event.type == "response.output_text.delta":
                                print(event.delta, end="", flush=True)
                            elif event.type == "error":
                                raise SystemExit("Erreur de flux : " + event.code)
                    print()
                else:
                    print(result.output_text)
    except APIError as error:
        raise SystemExit("Échec de l'appel à la gateway : " + str(error)) from None


if __name__ == "__main__":
    main()
