import argparse
import asyncio
import json
import sys
import time

from .config import Settings
from .errors import GatewayError
from .oauth import login, logout
from .storage import Storage, private_write


def main():
    parser = argparse.ArgumentParser(description="Passerelle personnelle — administration locale uniquement")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    auth = sub.add_parser("login")
    auth.add_argument("--account", help="ID de l'inscription sauvegardée, ou new")
    auth.add_argument("--no-browser", action="store_true")
    sub.add_parser("logout")
    sub.add_parser("accounts")
    select = sub.add_parser("select-account")
    select.add_argument("client_id")
    key = sub.add_parser("key-create")
    key.add_argument("--name", default="client")
    key.add_argument("--model", action="append", default=[])
    key.add_argument("--days", type=int, default=90)
    key.add_argument("--daily-limit", type=int, default=1000)
    sub.add_parser("keys")
    sub.add_parser("models")
    sub.add_parser("preferences")
    sub.add_parser("choose")
    default = sub.add_parser("set-default")
    default.add_argument("--model", required=True)
    default.add_argument(
        "--effort",
        choices=["auto", "instant", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
        default="auto",
    )
    revoke = sub.add_parser("key-revoke")
    revoke.add_argument("id")
    serve = sub.add_parser("serve")
    serve.add_argument("--provider", choices=["chatgpt_oauth", "ollama"])
    serve.add_argument("--port", type=int)
    state_import = sub.add_parser("import-state")
    state_import.add_argument("--stdin", action="store_true", required=True)
    sub.add_parser("export-state")
    args = parser.parse_args()
    settings = Settings()
    storage = Storage(settings.data_dir)
    try:
        if args.command == "init":
            key_file = storage.initialize()
            print("Clé client disponible dans : " + str(key_file))
        elif args.command == "login":
            print(json.dumps(login(storage, args.account, not args.no_browser), ensure_ascii=False))
        elif args.command == "logout":
            confirmed = logout(storage)
            print(
                "Déconnecté. Révocation distante "
                + ("confirmée." if confirmed else "non confirmée ; vérifier les réglages ChatGPT.")
            )
        elif args.command == "accounts":
            state = storage.read_secrets()
            print(
                json.dumps(
                    [
                        {
                            "client_id": key,
                            "email": value.get("email"),
                            "active": key == state["active"],
                            "connected": bool(value.get("access_token")),
                            "plan_usage": "chatgpt.tokens.use.direct" in value.get("scopes", []),
                        }
                        for key, value in state["accounts"].items()
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        elif args.command == "select-account":
            from filelock import FileLock

            with FileLock(str(storage.root / "oauth.lock")):
                state = storage.read_secrets()
                if args.client_id not in state["accounts"]:
                    raise ValueError("Inscription inconnue")
                state["active"] = args.client_id
                storage.write_secrets(state)
            print("Compte actif sélectionné.")
        elif args.command == "key-create":
            if args.days <= 0 or args.daily_limit <= 0:
                raise ValueError("Les limites doivent être positives")
            key_id, key = storage.issue_key(
                args.name, args.model, time.time() + args.days * 86400, args.daily_limit
            )
            path = storage.root / (key_id + ".key")
            private_write(path, (key + "\n").encode())
            print("Nouvelle clé " + key_id + " écrite dans " + str(path))
        elif args.command == "keys":
            print(json.dumps(storage.list_keys(), indent=2))
        elif args.command in {"models", "set-default"}:
            from .selection import load_catalog, validate_selection

            catalog = asyncio.run(load_catalog(settings, storage))
            if args.command == "models":
                print(json.dumps(catalog, indent=2, ensure_ascii=False))
            else:
                effort = (
                    None if args.effort == "auto" else "none" if args.effort == "instant" else args.effort
                )
                validate_selection(catalog, args.model, effort, settings.provider)
                storage.set_preferences(settings.provider, args.model, effort)
                print(
                    "Sélection enregistrée : "
                    + args.model
                    + " / "
                    + args.effort
                    + ". Modèle client : passerelle-default"
                )
        elif args.command == "preferences":
            print(
                json.dumps(
                    {
                        "provider": settings.provider,
                        "model_alias": "passerelle-default",
                        "selection": storage.preferences(settings.provider),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "choose":
            from .chooser import choose

            choose(settings, storage)
        elif args.command == "export-state":
            from .migration import export_state

            if sys.stdout.isatty():
                raise GatewayError(
                    400,
                    "refuse_terminal_export",
                    "Exporter uniquement vers un fichier privé ou un canal SSH, jamais vers le terminal.",
                )
            sys.stdout.write(export_state(storage))
        elif args.command == "import-state":
            from .migration import import_state

            import_state(storage, sys.stdin.read(16_777_217))
            print("Session et clés importées. Le host ID du serveur est conservé.")
        elif args.command == "key-revoke":
            print("Révoquée." if storage.revoke_key(args.id) else "Clé inconnue.")
        elif args.command == "serve":
            import logging
            from logging.handlers import RotatingFileHandler

            import uvicorn

            from .app import create_app

            storage.initialize()

            if args.provider:
                settings.provider = args.provider
            if args.port:
                settings.port = args.port
            audit_logger = logging.getLogger("passerelle")
            audit_logger.setLevel(logging.INFO)
            audit_logger.propagate = False
            audit_logger.addHandler(
                RotatingFileHandler(
                    storage.root / "http.jsonl", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
                )
            )
            uvicorn.run(
                create_app(settings),
                host=settings.host,
                port=settings.port,
                workers=1,
                proxy_headers=False,
                access_log=False,
                log_level="info",
            )
    except Exception as exc:
        print(
            exc.message
            if isinstance(exc, GatewayError)
            else "Opération refusée ou indisponible (aucun secret affiché).",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
