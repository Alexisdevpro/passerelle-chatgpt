"""Private state transfer over SSH. No credentials belong in the image or source archive."""

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing

from cryptography.fernet import Fernet
from filelock import FileLock

from .errors import GatewayError
from .storage import private_write

MAX_TRANSFER_BYTES = 16_777_216


def export_state(storage):
    with FileLock(str(storage.root / "oauth.lock"), timeout=30):
        state = storage.read_secrets()
        if not state.get("active") or not state.get("accounts", {}).get(state["active"], {}).get(
            "access_token"
        ):
            raise GatewayError(400, "no_active_session", "Aucune session ChatGPT active à transférer.")
        snapshot = sqlite3.connect(":memory:")
        try:
            with storage.connect() as source:
                source.backup(snapshot)
            database = snapshot.serialize()
        finally:
            snapshot.close()
        bundle = {
            "version": 1,
            "database": base64.b64encode(database).decode(),
            "accounts": base64.b64encode((storage.root / "accounts.enc").read_bytes()).decode(),
            "master_key": base64.b64encode((storage.root / "master.key").read_bytes()).decode(),
            "client_key": base64.b64encode((storage.root / "client-key.txt").read_bytes()).decode(),
        }
        result = json.dumps(bundle)
        if len(result.encode()) > MAX_TRANSFER_BYTES:
            raise GatewayError(413, "state_too_large", "État trop volumineux pour ce transfert.")
        return result


def import_state(storage, raw):
    if len(raw.encode()) > MAX_TRANSFER_BYTES:
        raise GatewayError(413, "state_too_large", "État trop volumineux pour ce transfert.")
    bundle = json.loads(raw)
    if (
        set(bundle) != {"version", "database", "accounts", "master_key", "client_key"}
        or bundle["version"] != 1
    ):
        raise ValueError("Invalid transfer format")
    values = {
        name: base64.b64decode(bundle[name], validate=True)
        for name in ("database", "accounts", "master_key", "client_key")
    }
    account_state = json.loads(Fernet(values["master_key"]).decrypt(values["accounts"]))
    active = account_state.get("accounts", {}).get(account_state.get("active"), {})
    if not active.get("access_token") or active.get("client_id") != account_state.get("active"):
        raise ValueError("Invalid active registration")
    if not {"chatgpt.tokens.use.direct", "resource.invoke"}.issubset(active.get("scopes", [])):
        raise ValueError("Missing ChatGPT plan scopes")
    key = values["client_key"].decode().strip()
    if not re.fullmatch(r"gw_[A-Za-z0-9_-]{30,}", key):
        raise ValueError("Invalid gateway key")
    snapshot = sqlite3.connect(":memory:")
    try:
        snapshot.deserialize(values["database"])
        if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Invalid database")
        if not snapshot.execute(
            "SELECT id FROM keys WHERE digest=?", (hashlib.sha256(key.encode()).hexdigest(),)
        ).fetchone():
            raise ValueError("Client key absent from database")
        for table in ("keys", "usage", "audit", "preferences"):
            snapshot.execute("SELECT * FROM " + table + " LIMIT 1")
        with FileLock(str(storage.root / "oauth.lock"), timeout=30):
            if storage.read_secrets().get("accounts"):
                raise GatewayError(
                    409,
                    "state_already_configured",
                    "Le serveur contient déjà une session. Aucun état remplacé.",
                )
            with storage.connect() as existing:
                if (
                    existing.execute("SELECT COUNT(*) FROM keys").fetchone()[0] > 1
                    or existing.execute("SELECT COALESCE(SUM(requests),0) FROM usage").fetchone()[0]
                ):
                    raise GatewayError(
                        409,
                        "state_already_configured",
                        "Le serveur contient déjà des clés ou de l'usage. Aucun état remplacé.",
                    )
            host_id = storage.host_id()
            fd, path = tempfile.mkstemp(prefix="migration-", suffix=".sqlite3", dir=storage.root)
            os.close(fd)
            try:
                with closing(sqlite3.connect(path)) as destination:
                    snapshot.backup(destination)
                private_write(storage.root / "master.key", values["master_key"])
                private_write(storage.root / "accounts.enc", values["accounts"])
                private_write(storage.root / "client-key.txt", values["client_key"])
                os.replace(path, storage.db_path)
            finally:
                if os.path.exists(path):
                    os.unlink(path)
            if storage.host_id() != host_id:
                raise RuntimeError("Host ID must be preserved")
    finally:
        snapshot.close()
