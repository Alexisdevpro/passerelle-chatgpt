import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from filelock import FileLock

from .errors import GatewayError


def private_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


class Storage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.root / "gateway.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS keys (
                  id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                  models TEXT NOT NULL, expires REAL, revoked INTEGER NOT NULL DEFAULT 0,
                  daily_limit INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS usage (
                  key_id TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL,
                  PRIMARY KEY(key_id, day));
                CREATE TABLE IF NOT EXISTS audit (
                  at REAL NOT NULL, action TEXT NOT NULL, key_id TEXT);
                CREATE TABLE IF NOT EXISTS preferences (
                  provider TEXT PRIMARY KEY, model TEXT NOT NULL, effort TEXT,
                  updated REAL NOT NULL);
            """)
        if os.name != "nt":
            self.db_path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with FileLock(str(self.root / "init.lock")):
            self.host_id()
            self.cipher()
            key_file = self.root / "client-key.txt"
            if not key_file.exists():
                _, key = self.issue_key("local-owner")
                private_write(key_file, (key + "\n").encode())
            return key_file

    def cipher(self):
        key_path = self.root / "master.key"
        with FileLock(str(self.root / "master.lock")):
            if not key_path.exists():
                private_write(key_path, Fernet.generate_key())
            return Fernet(key_path.read_bytes())

    def read_secrets(self):
        path = self.root / "accounts.enc"
        if not path.exists():
            return {"active": None, "accounts": {}}
        try:
            return json.loads(self.cipher().decrypt(path.read_bytes()))
        except Exception as exc:
            raise GatewayError(
                503, "credential_store_unavailable", "Stockage de connexion illisible."
            ) from exc

    def write_secrets(self, state):
        private_write(self.root / "accounts.enc", self.cipher().encrypt(json.dumps(state).encode()))

    def host_id(self):
        path = self.root / "host-id"
        with FileLock(str(self.root / "host.lock")):
            if not path.exists():
                private_write(path, ("urn:uuid:" + str(uuid.uuid4())).encode())
            return path.read_text().strip()

    def issue_key(self, name, models=None, expires=None, daily_limit=1000):
        raw = "gw_" + secrets.token_urlsafe(32)
        key_id = "key_" + secrets.token_hex(8)
        with self.connect() as db:
            db.execute(
                "INSERT INTO keys VALUES (?,?,?,?,?,0,?,?)",
                (
                    key_id,
                    hashlib.sha256(raw.encode()).hexdigest(),
                    name,
                    json.dumps(models or []),
                    expires,
                    daily_limit,
                    time.time(),
                ),
            )
            db.execute("INSERT INTO audit VALUES (?,?,?)", (time.time(), "key_created", key_id))
        return key_id, raw

    def authenticate(self, raw):
        if not raw or not raw.startswith("gw_") or len(raw) > 200:
            raise GatewayError(401, "invalid_api_key", "Clé de passerelle absente ou invalide.")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM keys WHERE digest=?", (hashlib.sha256(raw.encode()).hexdigest(),)
            ).fetchone()
        if not row or row["revoked"] or (row["expires"] is not None and row["expires"] <= time.time()):
            raise GatewayError(401, "invalid_api_key", "Clé de passerelle absente ou invalide.")
        return dict(row)

    def reserve_request(self, key):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO usage VALUES (?,?,0)", (key["id"], day))
            row = db.execute(
                "SELECT requests FROM usage WHERE key_id=? AND day=?", (key["id"], day)
            ).fetchone()
            if row["requests"] >= key["daily_limit"]:
                raise GatewayError(429, "daily_request_limit", "Quota journalier local de cette clé atteint.")
            db.execute("UPDATE usage SET requests=requests+1 WHERE key_id=? AND day=?", (key["id"], day))

    def revoke_key(self, key_id):
        with self.connect() as db:
            result = db.execute("UPDATE keys SET revoked=1 WHERE id=?", (key_id,))
            db.execute("INSERT INTO audit VALUES (?,?,?)", (time.time(), "key_revoked", key_id))
            return result.rowcount > 0

    def list_keys(self):
        with self.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT id,name,models,expires,revoked,daily_limit FROM keys")
            ]

    def preferences(self, provider):
        with self.connect() as db:
            row = db.execute("SELECT model,effort FROM preferences WHERE provider=?", (provider,)).fetchone()
        return dict(row) if row else None

    def set_preferences(self, provider, model, effort=None):
        with self.connect() as db:
            db.execute(
                """INSERT INTO preferences VALUES (?,?,?,?)
                ON CONFLICT(provider) DO UPDATE SET model=excluded.model,
                effort=excluded.effort, updated=excluded.updated""",
                (provider, model, effort, time.time()),
            )
            db.execute("INSERT INTO audit VALUES (?,?,NULL)", (time.time(), "preferences_changed"))
