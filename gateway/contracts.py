from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .errors import GatewayError
from .images import validate_image_count, validate_image_url

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("store", mode="before", check_fields=False)
    @classmethod
    def store_false_only(cls, value):
        if value is not False:
            raise ValueError("store must be the boolean false")
        return value

    @field_validator("n", mode="before", check_fields=False)
    @classmethod
    def one_choice_only(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("n must be the integer 1")
        return value


class InputText(StrictModel):
    type: Literal["input_text"]
    text: StrictStr


ImageDetail = Literal["auto", "low", "high", "original"]


class InputImage(StrictModel):
    type: Literal["input_image"]
    image_url: StrictStr = Field(
        description="URL HTTPS accessible à OpenAI ou data URL base64 JPEG/PNG/WEBP/GIF. 10 Mio maximum par image intégrée ; 8 images et 16 Mio de JSON par requête."
    )
    detail: ImageDetail = "auto"

    _validate_url = field_validator("image_url")(validate_image_url)


InputPart = Annotated[InputText | InputImage, Field(discriminator="type")]


class Message(StrictModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: StrictStr | list[InputPart]
    type: Literal["message"] | None = None

    @model_validator(mode="after")
    def user_images_only(self):
        if (
            self.role != "user"
            and not isinstance(self.content, str)
            and any(isinstance(p, InputImage) for p in self.content)
        ):
            raise GatewayError(
                400, "unsupported_parameter", "Les images doivent être dans un message user.", "input"
            )
        return self


class Reasoning(StrictModel):
    effort: ReasoningEffort


class ResponseRequest(StrictModel):
    model: StrictStr = Field(min_length=1, max_length=200)
    input: StrictStr | list[Message]
    instructions: StrictStr | None = None
    stream: StrictBool = False
    store: Literal[False] = False
    reasoning: Reasoning | None = None
    temperature: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=2)
    top_p: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=1)
    max_output_tokens: StrictInt | None = Field(default=None, ge=1, le=131072)

    @model_validator(mode="after")
    def image_count(self):
        if not isinstance(self.input, str):
            validate_image_count(self.input)
        return self


class ChatText(StrictModel):
    type: Literal["text"]
    text: StrictStr


class ChatImageURL(StrictModel):
    url: StrictStr = Field(
        description="URL HTTPS ou data URL base64 d'une photo. Même contenu que Responses input_image.image_url."
    )
    detail: ImageDetail = "auto"

    _validate_url = field_validator("url")(validate_image_url)


class ChatImage(StrictModel):
    type: Literal["image_url"]
    image_url: ChatImageURL


ChatPart = Annotated[ChatText | ChatImage, Field(discriminator="type")]


class ChatMessage(StrictModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: StrictStr | list[ChatPart]

    @model_validator(mode="after")
    def user_images_only(self):
        if (
            self.role != "user"
            and not isinstance(self.content, str)
            and any(isinstance(p, ChatImage) for p in self.content)
        ):
            raise GatewayError(
                400, "unsupported_parameter", "Les images doivent être dans un message user.", "messages"
            )
        return self


class StreamOptions(StrictModel):
    include_usage: StrictBool = False


class TextFormat(StrictModel):
    type: Literal["text"]


class ChatRequest(StrictModel):
    model: StrictStr = Field(min_length=1, max_length=200)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: StrictBool = False
    stream_options: StreamOptions | None = None
    store: Literal[False] = False
    n: Literal[1] = 1
    temperature: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=2)
    top_p: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=1)
    max_tokens: StrictInt | None = Field(default=None, ge=1, le=131072)
    max_completion_tokens: StrictInt | None = Field(default=None, ge=1, le=131072)
    reasoning_effort: ReasoningEffort | None = None
    response_format: TextFormat | None = None

    @model_validator(mode="after")
    def image_count(self):
        validate_image_count(self.messages)
        return self


def text_content(content):
    return content if isinstance(content, str) else "".join(part.text for part in content)


def response_content(content):
    if isinstance(content, str) or not any(isinstance(part, InputImage) for part in content):
        return text_content(content)
    return [part.model_dump() for part in content]


def chat_input_content(content):
    if isinstance(content, str) or not any(isinstance(part, ChatImage) for part in content):
        return text_content(content)
    return [
        {"type": "input_image", "image_url": part.image_url.url, "detail": part.image_url.detail}
        if isinstance(part, ChatImage)
        else {"type": "input_text", "text": part.text}
        for part in content
    ]


def normalized_input(request: ResponseRequest):
    if isinstance(request.input, str):
        messages = [{"role": "user", "content": request.input}]
    else:
        messages = [{"role": m.role, "content": response_content(m.content)} for m in request.input]
    if not messages or not any(m["content"] for m in messages):
        raise GatewayError(400, "invalid_input", "L'entrée doit contenir du texte ou une image.", "input")
    systems = []
    ordinary = []
    for message in messages:
        if message["role"] == "system":
            if ordinary:
                raise GatewayError(
                    400,
                    "unsupported_parameter",
                    "Les messages system doivent précéder les autres messages.",
                    "input",
                )
            systems.append(message["content"])
        else:
            ordinary.append(message)
    if systems and request.instructions is not None:
        raise GatewayError(
            400,
            "unsupported_parameter",
            "Utilisez instructions ou des messages system, sans les combiner.",
            "instructions",
        )
    instructions = "\n\n".join(systems) if systems else request.instructions
    if not ordinary:
        raise GatewayError(400, "invalid_input", "Ajoutez au moins un message non-system.", "input")
    return ordinary, instructions


def chat_to_response(chat: ChatRequest):
    if chat.max_tokens is not None and chat.max_completion_tokens is not None:
        raise GatewayError(400, "invalid_parameter", "Utilisez un seul plafond de tokens.", "max_tokens")
    if chat.stream_options is not None and not chat.stream:
        raise GatewayError(400, "invalid_parameter", "stream_options exige stream:true.", "stream_options")
    data = {
        "model": chat.model,
        "input": [{"role": m.role, "content": chat_input_content(m.content)} for m in chat.messages],
        "stream": chat.stream,
        "store": False,
    }
    for field in ("temperature", "top_p"):
        if getattr(chat, field) is not None:
            data[field] = getattr(chat, field)
    if chat.max_tokens is not None or chat.max_completion_tokens is not None:
        data["max_output_tokens"] = (
            chat.max_completion_tokens if chat.max_completion_tokens is not None else chat.max_tokens
        )
    if chat.reasoning_effort is not None:
        data["reasoning"] = {"effort": chat.reasoning_effort}
    return ResponseRequest.model_validate(data)


def chat_usage(response):
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    fields = [usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens")]
    if any(type(item) is not int or item < 0 for item in fields):
        return None
    return {"prompt_tokens": fields[0], "completion_tokens": fields[1], "total_tokens": fields[2]}


def chat_finish_reason(response):
    if response["status"] == "completed":
        return "stop"
    reason = (response.get("incomplete_details") or {}).get("reason")
    if reason == "max_output_tokens":
        return "length"
    if reason == "content_filter":
        return "content_filter"
    raise GatewayError(502, "unsupported_finish_reason", "Motif de réponse incomplète non pris en charge.")


def response_to_chat(response):
    text = "".join(
        part.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    refusals = "".join(
        part.get("refusal", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "refusal"
    )
    reason = chat_finish_reason(response)
    return {
        "id": "chatcmpl-" + response["id"],
        "object": "chat.completion",
        "created": int(response["created_at"]),
        "model": response["model"],
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text if text or not refusals else None,
                    "refusal": refusals or None,
                },
                "logprobs": None,
                "finish_reason": reason,
            }
        ],
        "usage": chat_usage(response),
    }
