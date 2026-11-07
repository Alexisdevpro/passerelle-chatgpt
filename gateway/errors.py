class GatewayError(Exception):
    def __init__(self, status, code, message, param=None, error_type=None):
        self.status = status
        self.code = code
        self.message = message
        self.param = param
        self.error_type = error_type or (
            "authentication_error"
            if status == 401
            else "rate_limit_error"
            if status == 429
            else "server_error"
            if status >= 500
            else "invalid_request_error"
        )

    def body(self):
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


SAFE_CODES = {
    "subscription_sharing_user_not_eligible": (403, "Compte ou espace non éligible au partage ChatGPT."),
    "subscription_sharing_usage_limit_exceeded": (
        429,
        "Limite de partage ChatGPT atteinte. Voir ChatGPT > Usage.",
    ),
    "subscription_sharing_usage_unavailable": (503, "Vérification du quota temporairement indisponible."),
    "subscription_sharing_unsupported_capability": (400, "Capacité refusée par le backend ChatGPT."),
    "subscription_sharing_route_not_supported": (403, "Route non autorisée par le backend ChatGPT."),
    "subscription_sharing_invalid_user": (401, "Session ChatGPT non acceptée. Reconnectez ce projet."),
    "chatpass_v2_scope_not_authorized": (403, "Permission ChatGPT insuffisante."),
    "chatpass_v2_invalid_authorization_context": (403, "Contexte d'autorisation ChatGPT invalide."),
    "subscription_sharing_user_unavailable": (503, "Compte ChatGPT temporairement indisponible."),
    "rate_limit_exceeded": (429, "Limite du backend atteinte."),
    "insufficient_quota": (429, "Quota du backend atteint."),
    "model_not_found": (404, "Modèle indisponible sur ce backend."),
    "context_length_exceeded": (400, "Contexte trop long pour ce modèle."),
}


def upstream_error(status, body=None):
    # Never reflect upstream messages, diagnostic detail, tokens or prompts.
    detail = body.get("error", {}) if isinstance(body, dict) else {}
    code = detail.get("code") if isinstance(detail, dict) else None
    if isinstance(code, str) and code in SAFE_CODES:
        expected, message = SAFE_CODES[code]
        param = detail.get("param")
        if (
            not isinstance(param, str)
            or not param.replace("_", "").replace(".", "").isalnum()
            or len(param) > 80
        ):
            param = None
        return GatewayError(status if 400 <= status < 600 else expected, code, message, param)
    status = status if status in {400, 401, 403, 404, 429, 502, 503, 504} else 502
    return GatewayError(status, "upstream_error", "Le backend a refusé ou interrompu la requête.")
