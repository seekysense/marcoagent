import asyncio
import os
import re
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

_WHISPER_MODEL_CACHE: dict[str, Any] = {}
_WHISPER_MODEL_LOCK = threading.Lock()
_LANGUAGE_ALIASES: dict[str, str] = {
    "it": "it",
    "italian": "it",
    "italiano": "it",
    "en": "en",
    "english": "en",
    "inglese": "en",
    "fr": "fr",
    "french": "fr",
    "francese": "fr",
    "es": "es",
    "spanish": "es",
    "spagnolo": "es",
    "de": "de",
    "german": "de",
    "tedesco": "de",
    "pt": "pt",
    "portuguese": "pt",
    "portoghese": "pt",
    "ru": "ru",
    "russian": "ru",
    "russo": "ru",
    "ar": "ar",
    "arabic": "ar",
    "arabo": "ar",
    "zh": "zh",
    "chinese": "zh",
    "cinese": "zh",
    "ja": "ja",
    "japanese": "ja",
    "giapponese": "ja",
    "ko": "ko",
    "korean": "ko",
    "coreano": "ko",
}


@dataclass(frozen=True)
class AudioTranscriptionResult:
    transcript: str
    local_path: str
    telegram_file_path: str


@dataclass(frozen=True)
class ImagePreprocessResult:
    image_bytes: bytes
    local_path: str
    telegram_file_path: str
    original_width: int
    original_height: int
    output_width: int
    output_height: int


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", value)


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _find_closing_paren(text: str, open_index: int) -> int:
    depth = 0
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_markdown_fragment(text: str) -> tuple[str, list[dict[str, Any]]]:
    entities: list[dict[str, Any]] = []
    out_parts: list[str] = []
    utf16_pos = 0
    i = 0

    def append_text(chunk: str) -> None:
        nonlocal utf16_pos
        if not chunk:
            return
        out_parts.append(chunk)
        utf16_pos += _utf16_len(chunk)

    while i < len(text):
        # fenced code block: ```lang\n...\n```
        if text.startswith("```", i):
            close = text.find("```", i + 3)
            if close != -1:
                block = text[i + 3 : close]
                language: str | None = None
                code_text = block
                if "\n" in block:
                    first_line, rest = block.split("\n", 1)
                    candidate = first_line.strip()
                    if candidate and " " not in candidate:
                        language = candidate
                        code_text = rest

                start = utf16_pos
                append_text(code_text)
                length = _utf16_len(code_text)
                if length > 0:
                    entity: dict[str, Any] = {"type": "pre", "offset": start, "length": length}
                    if language:
                        entity["language"] = language
                    entities.append(entity)
                i = close + 3
                continue

        # inline code: `...`
        if text[i] == "`":
            close = text.find("`", i + 1)
            if close != -1:
                code_text = text[i + 1 : close]
                start = utf16_pos
                append_text(code_text)
                length = _utf16_len(code_text)
                if length > 0:
                    entities.append({"type": "code", "offset": start, "length": length})
                i = close + 1
                continue

        # markdown link: [label](url)
        if text[i] == "[":
            close_bracket = text.find("]", i + 1)
            if close_bracket != -1 and close_bracket + 1 < len(text) and text[close_bracket + 1] == "(":
                close_paren = _find_closing_paren(text, close_bracket + 1)
                if close_paren != -1:
                    label_src = text[i + 1 : close_bracket]
                    url = text[close_bracket + 2 : close_paren].strip()
                    label_plain, _ = _parse_markdown_fragment(label_src)
                    start = utf16_pos
                    append_text(label_plain)
                    length = _utf16_len(label_plain)
                    if url and length > 0:
                        entities.append({"type": "text_link", "offset": start, "length": length, "url": url})
                    i = close_paren + 1
                    continue

        matched = False
        for delimiter, entity_type in (
            ("||", "spoiler"),
            ("**", "bold"),
            ("__", "underline"),
            ("~~", "strikethrough"),
            ("_", "italic"),
        ):
            if text.startswith(delimiter, i):
                close = text.find(delimiter, i + len(delimiter))
                if close != -1:
                    inner_src = text[i + len(delimiter) : close]
                    inner_plain, inner_entities = _parse_markdown_fragment(inner_src)
                    start = utf16_pos
                    append_text(inner_plain)
                    length = _utf16_len(inner_plain)

                    # Keep nested entities with shifted offsets.
                    for inner in inner_entities:
                        shifted = dict(inner)
                        shifted["offset"] = int(shifted["offset"]) + start
                        entities.append(shifted)

                    if length > 0:
                        entities.append({"type": entity_type, "offset": start, "length": length})

                    i = close + len(delimiter)
                    matched = True
                break
        if matched:
            continue

        if text[i] == "\\" and i + 1 < len(text):
            append_text(text[i + 1])
            i += 2
            continue

        append_text(text[i])
        i += 1

    return "".join(out_parts), entities


def parse_markdown_to_telegram_entities(markdown_text: str) -> tuple[str, list[dict[str, Any]]]:
    plain_text, entities = _parse_markdown_fragment(markdown_text or "")
    entities.sort(key=lambda e: (int(e.get("offset", 0)), int(e.get("length", 0))))
    return plain_text, entities


def markdown_to_telegram_payload(markdown_text: str) -> dict[str, Any]:
    plain_text, entities = parse_markdown_to_telegram_entities(markdown_text)
    payload: dict[str, Any] = {"text": plain_text}
    if entities:
        payload["entities"] = entities
    return payload


def _resolve_temp_dir(preferred: str, logger) -> Path:
    preferred_path = Path(preferred)
    try:
        preferred_path.mkdir(parents=True, exist_ok=True)
        probe = preferred_path / ".probe_write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return preferred_path
    except Exception as exc:
        fallback = Path("/tmp")
        fallback.mkdir(parents=True, exist_ok=True)
        if logger:
            logger.warning("cannot use temp dir %s (%s), fallback to %s", preferred_path, exc, fallback)
        return fallback


def _get_transit_temp_dir(kind: str) -> str:
    default_dir = (os.getenv("TRANSIT_TEMP_DIR") or "./temp").strip() or "./temp"
    if kind == "audio":
        value = (os.getenv("AUDIO_TEMP_DIR") or "").strip()
        return value or default_dir
    if kind == "image":
        value = (os.getenv("IMAGE_TEMP_DIR") or "").strip()
        return value or default_dir
    return default_dir


def normalize_whisper_language(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    return _LANGUAGE_ALIASES.get(raw, raw)


def _get_int_env(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def _load_whisper_model(model_name: str, logger):
    with _WHISPER_MODEL_LOCK:
        if model_name in _WHISPER_MODEL_CACHE:
            return _WHISPER_MODEL_CACHE[model_name]

        try:
            import whisper  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "Whisper non disponibile. Installa dipendenze con `pip install -r requirements.txt`"
            ) from exc

        if logger:
            logger.info("loading local whisper model=%s", model_name)
        model = whisper.load_model(model_name)
        _WHISPER_MODEL_CACHE[model_name] = model
        return model


def _transcribe_sync(audio_path: Path, model_name: str, language: str | None, logger) -> str:
    model = _load_whisper_model(model_name=model_name, logger=logger)
    kwargs: dict[str, Any] = {"fp16": False}
    if language:
        kwargs["language"] = language

    result = model.transcribe(str(audio_path), **kwargs)
    transcript = str(result.get("text", "")).strip()
    if not transcript:
        raise RuntimeError("Trascrizione vuota")
    return transcript


async def _download_telegram_file(
    token: str,
    file_id: str,
    update_id: int | None,
    temp_dir: Path,
    prefix: str,
    logger,
    timeout_seconds: int = 60,
) -> tuple[Path, str]:
    get_file_url = f"https://api.telegram.org/bot{token}/getFile"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.get(get_file_url, params={"file_id": file_id})
        response.raise_for_status()
        payload = response.json()

        if not payload.get("ok"):
            raise RuntimeError(f"getFile failed: {payload.get('description', 'unknown error')}")

        telegram_file_path = str(payload.get("result", {}).get("file_path", "")).strip()
        if not telegram_file_path:
            raise RuntimeError("file_path mancante in getFile")

        file_name = _safe_name(Path(telegram_file_path).name)
        update_part = str(update_id) if update_id is not None else "no_update"
        local_name = f"{prefix}_{update_part}_{_safe_name(file_id[-10:])}_{file_name}"
        local_path = temp_dir / local_name

        file_url = f"https://api.telegram.org/file/bot{token}/{telegram_file_path}"
        file_response = await client.get(file_url)
        file_response.raise_for_status()
        local_path.write_bytes(file_response.content)

        if logger:
            logger.info("file saved path=%s", local_path)
        return local_path, telegram_file_path


def _load_pillow_image_module():
    try:
        from PIL import Image as PILImage  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Pillow non disponibile. Installa dipendenze con `pip install -r requirements.txt`"
        ) from exc
    return PILImage


def _preprocess_image_sync(local_path: Path, max_dim: int, jpeg_quality: int) -> tuple[bytes, int, int, int, int]:
    PILImage = _load_pillow_image_module()

    with PILImage.open(local_path) as img:
        img = img.convert("RGB")
        original_width, original_height = img.size

        longest_side = max(original_width, original_height)
        if longest_side > max_dim:
            scale = max_dim / float(longest_side)
            output_width = max(1, int(round(original_width * scale)))
            output_height = max(1, int(round(original_height * scale)))
            img = img.resize((output_width, output_height), PILImage.Resampling.LANCZOS)
        else:
            output_width, output_height = original_width, original_height

        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=jpeg_quality, optimize=True)
        return buffer.getvalue(), original_width, original_height, output_width, output_height


async def transcribe_telegram_audio(
    token: str,
    file_id: str,
    update_id: int | None,
    preferred_language: str | None = None,
    logger=None,
) -> AudioTranscriptionResult:
    temp_dir = _resolve_temp_dir(_get_transit_temp_dir("audio"), logger=logger)
    whisper_model = os.getenv("WHISPER_MODEL", "base").strip() or "base"
    memory_language = normalize_whisper_language(preferred_language)
    env_language = normalize_whisper_language((os.getenv("WHISPER_LANGUAGE") or "").strip() or None)
    whisper_language = memory_language or env_language

    local_path, telegram_file_path = await _download_telegram_file(
        token=token,
        file_id=file_id,
        update_id=update_id,
        temp_dir=temp_dir,
        prefix="audio",
        logger=logger,
    )

    transcript = await asyncio.to_thread(
        _transcribe_sync,
        local_path,
        whisper_model,
        whisper_language,
        logger,
    )

    return AudioTranscriptionResult(
        transcript=transcript,
        local_path=str(local_path),
        telegram_file_path=telegram_file_path,
    )


async def preprocess_telegram_image_for_llm(
    token: str,
    file_id: str,
    update_id: int | None,
    logger=None,
) -> ImagePreprocessResult:
    temp_dir = _resolve_temp_dir(_get_transit_temp_dir("image"), logger=logger)
    max_dim = _get_int_env("IMAGE_MAX_DIM_PX", 1000, 128, 4096)
    jpeg_quality = _get_int_env("IMAGE_JPEG_QUALITY", 80, 30, 95)

    local_path, telegram_file_path = await _download_telegram_file(
        token=token,
        file_id=file_id,
        update_id=update_id,
        temp_dir=temp_dir,
        prefix="image",
        logger=logger,
    )

    image_bytes, orig_w, orig_h, out_w, out_h = await asyncio.to_thread(
        _preprocess_image_sync,
        local_path,
        max_dim,
        jpeg_quality,
    )

    return ImagePreprocessResult(
        image_bytes=image_bytes,
        local_path=str(local_path),
        telegram_file_path=telegram_file_path,
        original_width=orig_w,
        original_height=orig_h,
        output_width=out_w,
        output_height=out_h,
    )
