"""Official OSS OAuth/PKCE only. Credentials belong to this application."""

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from filelock import FileLock

from .errors import GatewayError
from .storage import Storage

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
UNUSABLE_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


def oidc_metadata():
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.get(ISSUER + "/.well-known/openid-configuration")
        response.raise_for_status()
        metadata = response.json()
    if metadata.get("issuer") != ISSUER:
        raise ValueError("Unexpected OIDC issuer")
    for field in ("jwks_uri", "revocation_endpoint"):
        if field in metadata:
            url = urlsplit(metadata[field])
            if url.scheme != "https" or url.hostname != "auth.openai.com" or url.username or url.password:
                raise ValueError("Unexpected OIDC endpoint")
    return metadata


def validate_id_token(token, client_id, nonce=None):
    metadata = oidc_metadata()
    header = jwt.get_unverified_header(token)
    if header.get("alg") not in {"RS256", "ES256"}:
        raise ValueError("Unsupported ID token signing algorithm")
    with httpx.Client(timeout=20, trust_env=False) as client:
        response = client.get(metadata["jwks_uri"])
        response.raise_for_status()
        keys = response.json()["keys"]
    key = next((entry for entry in keys if entry.get("kid") == header.get("kid")), None)
    if not key:
        raise ValueError("Unknown ID token signing key")
    claims = jwt.decode(
        token,
        jwt.PyJWK.from_dict(key).key,
        algorithms=[header["alg"]],
        audience=client_id,
        issuer=ISSUER,
        leeway=10,
        options={"require": ["iss", "aud", "exp", "iat", "sub"]},
    )
    if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("Invalid OIDC nonce")
    if not isinstance(claims["sub"], str) or not claims["sub"]:
        raise ValueError("Missing account identity")
    if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
        raise ValueError("Invalid authorized party")
    return claims


def token_record(tokens, client_id, previous=None, nonce=None):
    previous = previous or {}
    claims = None
    if tokens.get("id_token"):
        claims = validate_id_token(tokens["id_token"], client_id, nonce)
        if previous.get("subject") and claims["sub"] != previous["subject"]:
            raise ValueError("Account identity changed")
    elif nonce is not None:
        raise ValueError("Missing initial ID token")
    if str(tokens.get("token_type", "")).lower() != "bearer" or not tokens.get("access_token"):
        raise ValueError("Invalid token response")
    if not tokens.get("refresh_token"):
        raise ValueError("Missing rotating refresh token")
    granted = tokens["scope"].split() if "scope" in tokens else previous.get("scopes", [])
    return {
        **previous,
        "client_id": client_id,
        "subject": claims["sub"] if claims else previous["subject"],
        "email": claims.get("email", "") if claims else previous.get("email", ""),
        "issuer": ISSUER,
        "id_token": tokens.get("id_token", previous.get("id_token")),
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "scopes": granted,
        "expires_at": time.time() + float(tokens["expires_in"]),
        "earliest_refresh_at": tokens.get("earliest_refresh_at"),
        "saved_at": time.time(),
    }


def login(storage: Storage, account=None, open_browser=True, timeout=300):
    # Registration IDs are the account picker choices; tokens never leave this module.
    with FileLock(str(storage.root / "oauth.lock"), timeout=10):
        state_store = storage.read_secrets()
        if account is None:
            account = state_store.get("active")
        previous = state_store["accounts"].get(account, {})
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # The callback query contains a one-time authorization code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path != "/auth/callback":
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            received = query.get("state", [""])[0]
            if not hmac.compare_digest(received, state) or any(len(v) != 1 for v in query.values()):
                self.send_error(400, "Invalid callback state")
                return
            outcome.update({key: value[0] for key, value in query.items()})
            body = b"Connexion recue. Vous pouvez revenir dans Codex ou le terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
        params = {
            "client_id": previous.get("client_id", "dynamic_agent_client"),
            "ext_agent_host_id": storage.host_id(),
            "response_type": "code",
            "redirect_uri": redirect,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if not previous:
            params["agent_name_hint"] = "Passerelle personnelle"
        elif previous.get("id_token"):
            params["id_token_hint"] = previous["id_token"]
        url = AUTHORIZE_URL + "?" + urlencode(params)

        # A local redirect avoids printing an authorization URL containing id_token_hint.
        class LocalStart(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path != "/continue/" + state:
                    self.send_error(404)
                    return
                self.send_response(302)
                self.send_header("Location", url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()

        with HTTPServer(("127.0.0.1", 0), LocalStart) as start_server:
            start_server.timeout = 0.1
            start_url = f"http://127.0.0.1:{start_server.server_port}/continue/{state}"
            print("Continue with ChatGPT : " + start_url, flush=True)
            if open_browser:
                webbrowser.open(start_url)
            deadline = time.monotonic() + timeout
            while not outcome and time.monotonic() < deadline:
                start_server.handle_request()
                server.handle_request()
    if not outcome:
        raise GatewayError(408, "oauth_timeout", "Connexion expirée. Relancez la connexion.")
    if outcome.get("error") or not outcome.get("code"):
        raise GatewayError(403, "oauth_declined", "Connexion ou partage ChatGPT refusé.")
    client_id = outcome.get("client_id", previous.get("client_id"))
    if not client_id or client_id == "dynamic_agent_client":
        raise ValueError("Registration did not return an issued client ID")
    if previous and client_id != previous["client_id"]:
        raise ValueError("Registration ID changed")
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": outcome["code"],
                "code_verifier": verifier,
                "redirect_uri": redirect,
                "resource": RESOURCE,
            },
        )
        if response.status_code != 200:
            raise GatewayError(401, "oauth_exchange_failed", "Échange OAuth refusé. Relancez une connexion.")
        record = token_record(response.json(), client_id, previous, nonce)
    with FileLock(str(storage.root / "oauth.lock"), timeout=10):
        current = storage.read_secrets()
        current["accounts"][client_id] = record
        current["active"] = client_id
        storage.write_secrets(current)
    if "chatgpt.tokens.use.direct" not in record["scopes"]:
        raise GatewayError(403, "plan_usage_disabled", "Connecté, mais partage de l'abonnement non autorisé.")
    return {"client_id": client_id, "email": record["email"], "plan_usage": True}


class OAuthSession:
    def __init__(self, storage):
        self.storage = storage
        self.lock = asyncio.Lock()

    def _get_token(self):
        # Cross-process lock: gateway, login and logout must not race rotating tokens.
        with FileLock(str(self.storage.root / "oauth.lock"), timeout=30):
            state = self.storage.read_secrets()
            record = state["accounts"].get(state.get("active"))
            if not record or not record.get("access_token"):
                raise GatewayError(503, "backend_not_connected", "Connectez ChatGPT avec la commande login.")
            if "chatgpt.tokens.use.direct" not in record.get(
                "scopes", []
            ) or "resource.invoke" not in record.get("scopes", []):
                raise GatewayError(403, "plan_usage_disabled", "Permission de partage ChatGPT absente.")
            if record["expires_at"] > time.time() + 60:
                return record["access_token"]
            earliest = record.get("earliest_refresh_at")
            if isinstance(earliest, str):
                try:
                    earliest = datetime.fromisoformat(earliest.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    earliest = None
            if isinstance(earliest, (int, float)) and earliest > time.time():
                if record["expires_at"] > time.time():
                    return record["access_token"]
                raise GatewayError(
                    503, "oauth_refresh_not_yet_allowed", "Renouvellement OAuth pas encore autorisé."
                )
            with httpx.Client(timeout=25, trust_env=False) as client:
                response = client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": record["client_id"],
                        "refresh_token": record["refresh_token"],
                        "resource": RESOURCE,
                    },
                )
            if response.status_code != 200:
                try:
                    code = response.json().get("error")
                    code = code.get("code") if isinstance(code, dict) else code
                except (ValueError, AttributeError):
                    code = None
                if code in UNUSABLE_REFRESH:
                    for field in ("access_token", "refresh_token", "id_token"):
                        record.pop(field, None)
                    self.storage.write_secrets(state)
                    raise GatewayError(
                        401,
                        "oauth_reauthentication_required",
                        "Session expirée ou révoquée. Reconnectez ChatGPT.",
                    )
                raise GatewayError(
                    503, "oauth_refresh_failed", "Renouvellement OAuth indisponible. Réessayez plus tard."
                )
            updated = token_record(response.json(), record["client_id"], record)
            state["accounts"][record["client_id"]] = updated
            self.storage.write_secrets(state)
            if (
                "chatgpt.tokens.use.direct" not in updated["scopes"]
                or "resource.invoke" not in updated["scopes"]
            ):
                raise GatewayError(403, "plan_usage_disabled", "Permission de partage ChatGPT retirée.")
            return updated["access_token"]

    async def token(self):
        async with self.lock:
            return await asyncio.to_thread(self._get_token)


def logout(storage, account=None):
    confirmed = False
    with FileLock(str(storage.root / "oauth.lock"), timeout=30):
        state = storage.read_secrets()
        account = account or state.get("active")
        record = state["accounts"].get(account, {})
        if record.get("refresh_token"):
            try:
                metadata = oidc_metadata()
                with httpx.Client(timeout=20, trust_env=False) as client:
                    for attempt in range(3):
                        response = client.post(
                            metadata["revocation_endpoint"],
                            data={
                                "token": record["refresh_token"],
                                "token_type_hint": "refresh_token",
                                "client_id": account,
                            },
                        )
                        if response.status_code == 200:
                            confirmed = True
                            break
                        if response.status_code < 500:
                            break
                        time.sleep(0.5 * 2**attempt)
            except Exception:
                confirmed = False
        for field in ("access_token", "refresh_token", "id_token"):
            record.pop(field, None)
        if state.get("active") == account:
            state["active"] = None
        storage.write_secrets(state)
    return confirmed
