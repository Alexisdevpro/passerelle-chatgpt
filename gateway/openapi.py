"""Core output schemas; native Responses extensions are passed through."""


def enrich(schema):
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    nullable_string = {"type": ["string", "null"]}
    components["GatewayError"] = {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["message", "type", "param", "code"],
                "properties": {
                    "message": {"type": "string"},
                    "type": {"type": "string"},
                    "code": nullable_string,
                    "param": nullable_string,
                },
            }
        },
    }
    components["GatewayModel"] = {
        "type": "object",
        "required": ["id", "object", "created", "owned_by"],
        "properties": {
            "id": {"type": "string"},
            "object": {"const": "model"},
            "created": {
                "type": "integer",
                "description": "0 : date de création non fournie par le catalogue",
            },
            "owned_by": {"type": "string"},
            "x_gateway": {
                "type": "object",
                "description": "Métadonnées de sélection : efforts annoncés, modèle cible de l'alias, réglage enregistré.",
            },
        },
    }
    components["GatewayResponse"] = {
        "type": "object",
        "additionalProperties": True,
        "required": ["id", "object", "created_at", "status", "model", "output"],
        "properties": {
            "id": {"type": "string"},
            "object": {"const": "response"},
            "created_at": {"type": "number"},
            "status": {"enum": ["completed", "incomplete"]},
            "model": {"type": "string"},
            "output": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Messages texte et éventuels items reasoning natifs",
                },
            },
            "usage": {"type": ["object", "null"], "description": "Consommation réelle, null si indisponible"},
        },
    }
    components["GatewayChatCompletion"] = {
        "type": "object",
        "required": ["id", "object", "created", "model", "choices", "usage"],
        "properties": {
            "id": {"type": "string"},
            "object": {"const": "chat.completion"},
            "created": {"type": "integer"},
            "model": {"type": "string"},
            "choices": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "required": ["index", "message", "finish_reason"],
                    "properties": {
                        "index": {"const": 0},
                        "finish_reason": {"enum": ["stop", "length", "content_filter"]},
                        "message": {
                            "type": "object",
                            "required": ["role", "content", "refusal"],
                            "properties": {
                                "role": {"const": "assistant"},
                                "content": nullable_string,
                                "refusal": nullable_string,
                            },
                        },
                    },
                },
            },
            "usage": {
                "type": ["object", "null"],
                "properties": {
                    "prompt_tokens": {"type": "integer"},
                    "completion_tokens": {"type": "integer"},
                    "total_tokens": {"type": "integer"},
                },
            },
        },
    }
    route_schemas = {
        "/v1/responses": {"$ref": "#/components/schemas/GatewayResponse"},
        "/v1/chat/completions": {"$ref": "#/components/schemas/GatewayChatCompletion"},
        "/v1/models": {
            "type": "object",
            "required": ["object", "data"],
            "properties": {
                "object": {"const": "list"},
                "data": {"type": "array", "items": {"$ref": "#/components/schemas/GatewayModel"}},
            },
        },
        "/v1/models/{model}": {"$ref": "#/components/schemas/GatewayModel"},
    }
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            if path in route_schemas:
                success = operation["responses"]["200"].setdefault("content", {})
                success["application/json"] = {"schema": route_schemas[path]}
                if path in {"/v1/responses", "/v1/chat/completions"}:
                    success["text/event-stream"] = {
                        "schema": {"type": "string"},
                        "example": "data: {...}\n\n",
                    }
            for code, error in operation["responses"].items():
                if int(code) >= 400:
                    error["content"] = {
                        "application/json": {"schema": {"$ref": "#/components/schemas/GatewayError"}}
                    }
    return schema
