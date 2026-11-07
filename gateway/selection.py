"""Local preferences and an explicit model alias; never override named model requests."""

import httpx

from .contracts import REASONING_EFFORTS, Reasoning
from .errors import GatewayError
from .providers import ChatGPTProvider, OllamaProvider

DEFAULT_ALIAS = "passerelle-default"


def selectable_efforts(model):
    metadata = model.get("x_gateway", {})
    return [value for value in metadata.get("reasoning_efforts", []) if value in REASONING_EFFORTS]


def validate_effort(model, effort, provider):
    if effort is None:
        return
    if effort not in REASONING_EFFORTS:
        raise GatewayError(
            400, "unsupported_reasoning_effort", "Effort de raisonnement invalide.", "reasoning.effort"
        )
    if provider == "ollama":
        raise GatewayError(
            400,
            "unsupported_reasoning_effort",
            "L'adaptateur Ollama ne traduit pas l'effort de raisonnement.",
            "reasoning.effort",
        )
    if model.get("x_gateway", {}).get("reasoning_efforts_known") and effort not in selectable_efforts(model):
        available = ", ".join(selectable_efforts(model)) or "automatique uniquement"
        raise GatewayError(
            400,
            "unsupported_reasoning_effort",
            "Effort non annoncé pour ce modèle. Choix disponibles : " + available + ".",
            "reasoning.effort",
        )


def validate_selection(catalog, model_id, effort, provider):
    if model_id == DEFAULT_ALIAS:
        raise GatewayError(
            400, "invalid_default_model", "Choisissez un modèle réel pour la sélection.", "model"
        )
    model = next((item for item in catalog if item["id"] == model_id), None)
    if model is None:
        raise GatewayError(404, "model_not_found", "Modèle absent du catalogue autorisé.", "model")
    validate_effort(model, effort, provider)
    return model


def default_alias(catalog, preferences):
    if not preferences or any(item["id"] == DEFAULT_ALIAS for item in catalog):
        return None
    target = next((item for item in catalog if item["id"] == preferences["model"]), None)
    if target is None:
        return None
    return {
        "id": DEFAULT_ALIAS,
        "object": "model",
        "created": 0,
        "owned_by": "passerelle",
        "x_gateway": {
            **target.get("x_gateway", {}),
            "target_model": target["id"],
            "selected_reasoning_effort": preferences["effort"],
        },
    }


def resolve_request(body, catalog, preferences, provider):
    if body.model == DEFAULT_ALIAS:
        if not preferences:
            raise GatewayError(
                400,
                "default_model_not_configured",
                "Ouvrez CHOISIR_MODELE.bat pour enregistrer votre choix.",
                "model",
            )
        if any(item["id"] == DEFAULT_ALIAS for item in catalog):
            raise GatewayError(
                400,
                "model_alias_conflict",
                "Le catalogue contient déjà l'identifiant réservé de l'alias.",
                "model",
            )
        effort = body.reasoning.effort if body.reasoning is not None else preferences["effort"]
        validate_selection(catalog, preferences["model"], effort, provider)
        return body.model_copy(
            update={
                "model": preferences["model"],
                "reasoning": Reasoning(effort=effort) if effort is not None else None,
            }
        )
    model = next((item for item in catalog if item["id"] == body.model), None)
    if model is None:
        raise GatewayError(404, "model_not_found", "Modèle absent du catalogue autorisé.", "model")
    validate_effort(model, body.reasoning.effort if body.reasoning else None, provider)
    return body


async def load_catalog(settings, storage):
    async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
        provider = (
            ChatGPTProvider(settings, client, storage)
            if settings.provider == "chatgpt_oauth"
            else OllamaProvider(settings, client)
        )
        return await provider.models()
