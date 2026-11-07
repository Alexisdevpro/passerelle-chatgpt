"""Bounded image references; remote URLs are forwarded, never fetched locally."""

import base64
import binascii
from urllib.parse import urlsplit

from .errors import GatewayError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 8
MAX_BASE64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def validate_image_url(value):
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator or not header.endswith(";base64") or header[5:-7] not in IMAGE_MIMES:
            raise GatewayError(
                400, "invalid_image_url", "Image base64 JPEG, PNG, WEBP ou GIF requise.", "image_url"
            )
        if len(encoded) > MAX_BASE64_CHARS:
            raise GatewayError(
                413, "image_too_large", "Une image base64 est limitée à 10 Mio avant encodage.", "image_url"
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise GatewayError(
                400, "invalid_image_url", "Encodage base64 de l'image invalide.", "image_url"
            ) from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise GatewayError(
                413, "image_too_large", "Une image base64 est limitée à 10 Mio avant encodage.", "image_url"
            )
        mime = header[5:-7]
        matches = {
            "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
            "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
            "image/gif": raw.startswith((b"GIF87a", b"GIF89a")),
        }
        if not matches[mime]:
            raise GatewayError(
                400,
                "invalid_image",
                "Le contenu de l'image ne correspond pas au format déclaré.",
                "image_url",
            )
        return value
    try:
        parsed = urlsplit(value)
        valid = (
            len(value) <= 8192
            and parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and not any(character.isspace() for character in value)
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise GatewayError(
            400, "invalid_image_url", "Fournir une URL HTTPS ou une data URL d'image base64.", "image_url"
        )
    return value


def validate_image_count(messages):
    count = sum(
        part.type in {"input_image", "image_url"}
        for message in messages
        if not isinstance(message.content, str)
        for part in message.content
    )
    if count > MAX_IMAGES:
        raise GatewayError(
            400, "too_many_images", "Huit images maximum par requête, historique inclus.", "input"
        )
