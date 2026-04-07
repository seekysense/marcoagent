import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv

from agent import AgentContext, build_agent_context, require_env
from agno.media import Image
from agno.os.app import AgentOS
from agno.os.interfaces.telegram import Telegram
from tools import reset_current_run_image_for_tools, set_current_run_image_for_tools
from utilities import (
    markdown_to_telegram_payload,
    normalize_whisper_language,
    preprocess_telegram_image_for_llm,
    transcribe_telegram_audio,
)

LOCAL_HOST = "127.0.0.1"
polling_task: asyncio.Task | None = None


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_effective_temp_dir(var_name: str) -> str:
    transit_default = (os.getenv("TRANSIT_TEMP_DIR") or "./temp").strip() or "./temp"
    value = (os.getenv(var_name) or "").strip()
    return value or transit_default


def _mask_secret(secret: str, visible_chars: int = 4) -> str:
    if len(secret) <= visible_chars * 2:
        return "***"
    return f"{secret[:visible_chars]}...{secret[-visible_chars:]}"


def _configure_logging() -> logging.Logger:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)
    logger_instance = logging.getLogger("telegram_bot")
    logger_instance.setLevel(level)

    if not logger_instance.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
        logger_instance.addHandler(handler)

    logger_instance.propagate = False
    return logger_instance


def _get_port() -> int:
    return int(os.getenv("PORT", "7777"))


def _get_telegram_mode() -> str:
    mode = os.getenv("TELEGRAM_MODE", "polling").strip().lower()
    return mode if mode in {"polling", "webhook"} else "polling"


def _get_polling_timeout_seconds() -> int:
    raw = os.getenv("TELEGRAM_POLLING_TIMEOUT_SECONDS", "15").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 15
    return max(1, value)


def _truncate(value: str, max_len: int = 120) -> str:
    clean = value.strip().replace("\n", " ")
    if len(clean) <= max_len:
        return clean
    return f"{clean[:max_len-3]}..."


def _extract_update_identity(update: dict) -> tuple[str | None, str | None, str | None, str | None]:
    message = update.get("message") or update.get("edited_message") or {}
    user_id_raw = (message.get("from") or {}).get("id")
    chat_id_raw = (message.get("chat") or {}).get("id")
    message_thread_id = message.get("message_thread_id")

    user_id = str(user_id_raw) if user_id_raw is not None else None
    chat_id = str(chat_id_raw) if chat_id_raw is not None else None
    thread_id = str(message_thread_id) if message_thread_id is not None else None
    text = message.get("text") or message.get("caption") or ""
    return user_id, chat_id, thread_id, text


def _build_session_scope(entity_id: str, chat_id: str | None, thread_id: str | None) -> str | None:
    if not chat_id:
        return None
    if thread_id:
        return f"tg:{entity_id}:{chat_id}:{thread_id}"
    return f"tg:{entity_id}:{chat_id}"


_MEMORY_LANGUAGE_HINTS: dict[str, tuple[str, ...]] = {
    "it": ("italiano", "italian", "italiana", "italiani"),
    "en": ("inglese", "english"),
    "fr": ("francese", "french"),
    "es": ("spagnolo", "spanish"),
    "de": ("tedesco", "german"),
    "pt": ("portoghese", "portuguese"),
}


def _get_user_preferred_language_from_memory(user_id: str | None) -> str | None:
    if not user_id or not _bool_env("WHISPER_USE_MEMORY_LANGUAGE", True):
        return None

    try:
        memories = agent_context.agent.get_user_memories(user_id=user_id) or []
    except Exception:
        logger.exception("unable to load user memories for language preference user_id=%s", user_id)
        return None

    if not memories:
        return None

    sorted_memories = sorted(memories, key=lambda m: getattr(m, "updated_at", 0) or 0, reverse=True)
    for memory in sorted_memories:
        parts: list[str] = []
        parts.append(str(getattr(memory, "memory", "") or ""))
        parts.append(str(getattr(memory, "input", "") or ""))
        topics = getattr(memory, "topics", None)
        if isinstance(topics, list):
            parts.extend(str(topic) for topic in topics)

        text = " ".join(parts).lower()
        if not text:
            continue

        # explicit code patterns like "lingua: it" or "language=en"
        code_match = re.search(r"\b(?:lingua|language|lang)\s*[:=]?\s*([a-z]{2})\b", text)
        if code_match:
            code = normalize_whisper_language(code_match.group(1))
            if code:
                logger.info("memory language detected user_id=%s lang=%s", user_id, code)
                return code

        for code, hints in _MEMORY_LANGUAGE_HINTS.items():
            if any(hint in text for hint in hints):
                logger.info("memory language detected user_id=%s lang=%s", user_id, code)
                return code

    return None


def _extract_audio_file(message: dict[str, Any]) -> tuple[str | None, str | None]:
    voice = message.get("voice")
    if isinstance(voice, dict) and voice.get("file_id"):
        return str(voice["file_id"]), "voice"

    audio = message.get("audio")
    if isinstance(audio, dict) and audio.get("file_id"):
        return str(audio["file_id"]), "audio"

    return None, None


def _extract_photo_file_id(message: dict[str, Any]) -> str | None:
    photos = message.get("photo")
    if not isinstance(photos, list) or not photos:
        return None

    valid_photos: list[dict[str, Any]] = [p for p in photos if isinstance(p, dict) and p.get("file_id")]
    if not valid_photos:
        return None

    best = max(
        valid_photos,
        key=lambda p: int(p.get("file_size") or (int(p.get("width", 0)) * int(p.get("height", 0)))),
    )
    return str(best["file_id"])


async def _send_telegram_message(
    token: str,
    chat_id: str | int,
    text: str,
    message_thread_id: str | int | None = None,
) -> None:
    formatted = markdown_to_telegram_payload(text or "")
    plain_text = str(formatted.get("text", ""))
    entities = formatted.get("entities") or []
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": plain_text,
    }
    if entities:
        payload["entities"] = entities
    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
        )

    try:
        data = response.json()
    except Exception:
        data = {"ok": False, "description": "non-json response"}

    if response.status_code != 200 or not data.get("ok"):
        logger.warning(
            "sendMessage failed status=%s description=%s (retry plain text)",
            response.status_code,
            data.get("description", ""),
        )
        if entities:
            retry_payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": plain_text,
            }
            if message_thread_id is not None:
                retry_payload["message_thread_id"] = int(message_thread_id)
            async with httpx.AsyncClient(timeout=20.0) as client:
                retry_response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json=retry_payload,
                )
            try:
                retry_data = retry_response.json()
            except Exception:
                retry_data = {"ok": False, "description": "non-json response"}

            if retry_response.status_code != 200 or not retry_data.get("ok"):
                logger.warning(
                    "sendMessage retry failed status=%s description=%s",
                    retry_response.status_code,
                    retry_data.get("description", ""),
                )


load_dotenv()

# Telegram webhook secret checks are strict outside development mode.
if not os.getenv("APP_ENV"):
    os.environ["APP_ENV"] = "development"

logger = _configure_logging()
agent_context: AgentContext = build_agent_context()
agent_id = agent_context.agent_id

# Accept the typo already present in the current .env for compatibility.
telegram_token = require_env("TELEGRAM_TOKEN", "TELEGHRAM_BOT_TOKEN")

agent_os = AgentOS(
    agents=[agent_context.agent],
    interfaces=[
        Telegram(
            agent=agent_context.agent,
            token=telegram_token,
            streaming=_bool_env("TELEGRAM_STREAMING", True),
        )
    ],
)
app = agent_os.get_app()


@app.middleware("http")
async def log_requests(request, call_next):
    started_at = time.perf_counter()
    method = request.method
    path = request.url.path
    query = request.url.query

    if path.startswith("/telegram"):
        logger.info(
            "incoming telegram request method=%s path=%s query=%s client=%s ua=%s",
            method,
            path,
            query,
            request.client.host if request.client else "unknown",
            request.headers.get("user-agent", "-"),
        )

    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.exception(
            "request failed method=%s path=%s query=%s took_ms=%.1f",
            method,
            path,
            query,
            elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    if path.startswith("/telegram"):
        logger.info(
            "telegram response method=%s path=%s status=%s took_ms=%.1f",
            method,
            path,
            response.status_code,
            elapsed_ms,
        )
    return response


async def log_telegram_webhook_info() -> None:
    if not _bool_env("CHECK_TELEGRAM_WEBHOOK_ON_STARTUP", True):
        return

    url = f"https://api.telegram.org/bot{telegram_token}/getWebhookInfo"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
        data = response.json()
    except Exception:
        logger.exception("could not fetch Telegram webhook info")
        return

    if response.status_code != 200 or not isinstance(data, dict):
        logger.warning("unexpected Telegram webhook response status=%s", response.status_code)
        return

    if not data.get("ok"):
        logger.warning("Telegram API getWebhookInfo failed: %s", data.get("description", "unknown error"))
        return

    info = data.get("result", {}) or {}
    logger.info(
        "telegram webhook url=%s pending_updates=%s last_error=%s",
        info.get("url", ""),
        info.get("pending_update_count", 0),
        info.get("last_error_message", ""),
    )


async def run_telegram_polling_loop() -> None:
    mode = _get_telegram_mode()
    if mode != "polling":
        return

    base_url = f"https://api.telegram.org/bot{telegram_token}"
    local_webhook_url = f"http://{LOCAL_HOST}:{_get_port()}/telegram/webhook"
    offset: int | None = None
    timeout_seconds = _get_polling_timeout_seconds()

    async with httpx.AsyncClient(timeout=25.0) as tg_client, httpx.AsyncClient(timeout=15.0) as local_client:
        if _bool_env("TELEGRAM_DELETE_WEBHOOK_ON_POLLING_START", True):
            try:
                response = await tg_client.post(
                    f"{base_url}/deleteWebhook",
                    params={"drop_pending_updates": "false"},
                )
                data = response.json()
                logger.info(
                    "polling bootstrap deleteWebhook status=%s ok=%s",
                    response.status_code,
                    data.get("ok"),
                )
            except Exception:
                logger.exception("polling bootstrap deleteWebhook failed")

        while True:
            params = {"timeout": timeout_seconds}
            if offset is not None:
                params["offset"] = offset

            try:
                response = await tg_client.get(f"{base_url}/getUpdates", params=params)
                payload = response.json()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("getUpdates request failed")
                await asyncio.sleep(2)
                continue

            if response.status_code != 200 or not payload.get("ok"):
                logger.warning(
                    "getUpdates non-ok status=%s description=%s",
                    response.status_code,
                    payload.get("description", ""),
                )
                await asyncio.sleep(2)
                continue

            updates = payload.get("result", [])
            if not updates:
                continue

            logger.info("polling received updates=%s", len(updates))
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1

                message_obj = update.get("message") or update.get("edited_message") or {}
                user_id_raw = (message_obj.get("from") or {}).get("id")
                user_id_for_memory = str(user_id_raw) if user_id_raw is not None else None
                chat_id_for_send = (message_obj.get("chat") or {}).get("id")
                thread_id_for_send = message_obj.get("message_thread_id")
                chat_id_for_scope = str(chat_id_for_send) if chat_id_for_send is not None else None
                thread_id_for_scope = str(thread_id_for_send) if thread_id_for_send is not None else None
                session_scope_for_media = _build_session_scope(agent_id, chat_id_for_scope, thread_id_for_scope)
                existing_text = (message_obj.get("text") or message_obj.get("caption") or "").strip()

                photo_file_id = _extract_photo_file_id(message_obj)
                if photo_file_id:
                    try:
                        processed_image = await preprocess_telegram_image_for_llm(
                            token=telegram_token,
                            file_id=photo_file_id,
                            update_id=update_id if isinstance(update_id, int) else None,
                            logger=logger,
                        )
                        image_prompt = existing_text or os.getenv(
                            "IMAGE_DEFAULT_PROMPT",
                            "Analizza l'immagine e rispondi in modo utile.",
                        )

                        logger.info(
                            "image preprocessing done update_id=%s file=%s local_path=%s original=%sx%s output=%sx%s",
                            update_id,
                            processed_image.telegram_file_path,
                            processed_image.local_path,
                            processed_image.original_width,
                            processed_image.original_height,
                            processed_image.output_width,
                            processed_image.output_height,
                        )

                        run_kwargs: dict[str, Any] = {
                            "stream": False,
                            "user_id": user_id_for_memory,
                            "add_history_to_context": _bool_env("IMAGE_ADD_HISTORY_TO_CONTEXT", False),
                            "images": [Image(content=processed_image.image_bytes)],
                        }
                        if session_scope_for_media:
                            run_kwargs["session_id"] = session_scope_for_media

                        image_context_token = set_current_run_image_for_tools(
                            image_bytes=processed_image.image_bytes,
                            local_path=processed_image.local_path,
                            telegram_file_path=processed_image.telegram_file_path,
                            user_id=user_id_for_memory,
                            session_id=session_scope_for_media,
                        )
                        try:
                            run_output = await agent_context.agent.arun(image_prompt, **run_kwargs)
                        finally:
                            reset_current_run_image_for_tools(image_context_token)
                        run_status = str(getattr(run_output, "status", ""))
                        run_content = str(getattr(run_output, "content", "") or "").strip()
                        logger.info(
                            "image direct-run completed update_id=%s status=%s has_content=%s",
                            update_id,
                            run_status,
                            bool(run_content),
                        )

                        if chat_id_for_send is not None:
                            if run_content:
                                await _send_telegram_message(
                                    token=telegram_token,
                                    chat_id=chat_id_for_send,
                                    text=run_content,
                                    message_thread_id=thread_id_for_send,
                                )
                            else:
                                await _send_telegram_message(
                                    token=telegram_token,
                                    chat_id=chat_id_for_send,
                                    text="Non sono riuscito a generare una risposta testuale dall'immagine.",
                                    message_thread_id=thread_id_for_send,
                                )
                    except Exception:
                        logger.exception("image pipeline failed update_id=%s", update_id)
                        if chat_id_for_send is not None:
                            await _send_telegram_message(
                                token=telegram_token,
                                chat_id=chat_id_for_send,
                                text="Errore durante la gestione dell'immagine. Riprova con un'altra immagine.",
                                message_thread_id=thread_id_for_send,
                            )
                    continue

                preferred_language = _get_user_preferred_language_from_memory(user_id_for_memory)
                audio_file_id, audio_kind = _extract_audio_file(message_obj)
                if audio_file_id:
                    try:
                        transcription = await transcribe_telegram_audio(
                            token=telegram_token,
                            file_id=audio_file_id,
                            update_id=update_id if isinstance(update_id, int) else None,
                            preferred_language=preferred_language,
                            logger=logger,
                        )
                        quoted_transcript = f'Trascrizione audio:\n"{transcription.transcript}"'

                        if chat_id_for_send is not None:
                            await _send_telegram_message(
                                token=telegram_token,
                                chat_id=chat_id_for_send,
                                text=quoted_transcript,
                                message_thread_id=thread_id_for_send,
                            )

                        logger.info(
                            "audio transcription done update_id=%s kind=%s lang=%s file=%s local_path=%s",
                            update_id,
                            audio_kind,
                            preferred_language or os.getenv("WHISPER_LANGUAGE", "auto"),
                            transcription.telegram_file_path,
                            transcription.local_path,
                        )

                        # For audio updates we run the agent directly.
                        # This avoids webhook/history-media side effects that can produce empty replies.
                        user_id = user_id_for_memory
                        chat_id = chat_id_for_scope
                        thread_id = thread_id_for_scope
                        session_scope = session_scope_for_media
                        agent_input = (
                            f"{existing_text}\n\n{quoted_transcript}" if existing_text else quoted_transcript
                        )
                        logger.info(
                            "audio direct-run update_id=%s user_id=%s session_scope=%s input=%s",
                            update_id,
                            user_id,
                            session_scope,
                            _truncate(agent_input, 220),
                        )

                        run_kwargs: dict[str, Any] = {
                            "stream": False,
                            "user_id": user_id,
                            "add_history_to_context": _bool_env("AUDIO_ADD_HISTORY_TO_CONTEXT", False),
                        }
                        if session_scope:
                            run_kwargs["session_id"] = session_scope

                        run_output = await agent_context.agent.arun(agent_input, **run_kwargs)
                        run_status = str(getattr(run_output, "status", ""))
                        run_content = str(getattr(run_output, "content", "") or "").strip()

                        logger.info(
                            "audio direct-run completed update_id=%s status=%s has_content=%s",
                            update_id,
                            run_status,
                            bool(run_content),
                        )

                        if chat_id_for_send is not None:
                            if run_content:
                                await _send_telegram_message(
                                    token=telegram_token,
                                    chat_id=chat_id_for_send,
                                    text=run_content,
                                    message_thread_id=thread_id_for_send,
                                )
                            else:
                                await _send_telegram_message(
                                    token=telegram_token,
                                    chat_id=chat_id_for_send,
                                    text="Non sono riuscito a generare una risposta testuale. Riprova con una domanda piu' specifica.",
                                    message_thread_id=thread_id_for_send,
                                )

                        # Audio update handled end-to-end: skip webhook forward.
                        continue
                    except Exception:
                        logger.exception("audio pipeline failed update_id=%s", update_id)
                        chat_id_for_send = (message_obj.get("chat") or {}).get("id")
                        thread_id_for_send = message_obj.get("message_thread_id")
                        if chat_id_for_send is not None:
                            await _send_telegram_message(
                                token=telegram_token,
                                chat_id=chat_id_for_send,
                                text="Errore durante la gestione dell'audio. Riprova con un nuovo vocale.",
                                message_thread_id=thread_id_for_send,
                            )
                        continue

                user_id, chat_id, thread_id, text = _extract_update_identity(update)
                session_scope = _build_session_scope(agent_id, chat_id, thread_id)
                logger.info(
                    "telegram update_id=%s user_id=%s session_scope=%s text=%s",
                    update_id,
                    user_id,
                    session_scope,
                    _truncate(text),
                )

                webhook_headers: dict[str, str] = {}
                webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "").strip()
                if webhook_secret:
                    webhook_headers["X-Telegram-Bot-Api-Secret-Token"] = webhook_secret

                try:
                    local_response = await local_client.post(
                        local_webhook_url,
                        json=update,
                        headers=webhook_headers or None,
                    )
                    webhook_status = ""
                    response_text = local_response.text
                    try:
                        local_payload = local_response.json()
                        webhook_status = str(local_payload.get("status", ""))
                    except Exception:
                        local_payload = {}
                        webhook_status = ""

                    logger.info(
                        "forwarded polled update_id=%s to /telegram/webhook status=%s webhook_status=%s body=%s",
                        update_id,
                        local_response.status_code,
                        webhook_status,
                        _truncate(response_text, 220),
                    )

                    # Fallback path for audio messages when webhook route ignores/rejects the update.
                    if audio_file_id and (
                        local_response.status_code != 200 or webhook_status in {"ignored", "", "error"}
                    ):
                        logger.warning(
                            "webhook did not process transcribed audio update_id=%s; using direct agent fallback",
                            update_id,
                        )
                        if text and user_id and chat_id:
                            try:
                                run_kwargs: dict[str, Any] = {"stream": False, "user_id": user_id}
                                if session_scope:
                                    run_kwargs["session_id"] = session_scope
                                run_output = await agent_context.agent.arun(text, **run_kwargs)
                                content = str(getattr(run_output, "content", "") or "").strip()
                                if content:
                                    await _send_telegram_message(
                                        token=telegram_token,
                                        chat_id=chat_id,
                                        text=content,
                                        message_thread_id=thread_id,
                                    )
                            except Exception:
                                logger.exception("direct agent fallback failed update_id=%s", update_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("failed forwarding update_id=%s to local webhook", update_id)


async def startup_diagnostics() -> None:
    logger.info(
        "startup app_env=%s llm_endpoint=%s llm_model=%s db_file=%s telegram_token=%s telegram_mode=%s polling_timeout=%ss agent_id=%s",
        os.getenv("APP_ENV", ""),
        agent_context.llm_endpoint,
        agent_context.llm_model,
        agent_context.db_file,
        _mask_secret(telegram_token),
        _get_telegram_mode(),
        _get_polling_timeout_seconds(),
        agent_id,
    )
    logger.info(
        "llm media settings send_media_to_model=%s store_media=%s",
        os.getenv("LLM_SEND_MEDIA_TO_MODEL", "true"),
        os.getenv("LLM_STORE_MEDIA", "false"),
    )
    logger.info("memory capture instructions: %s", _truncate(agent_context.memory_capture_instructions, 220))
    if agent_context.memory_additional_instructions:
        logger.info(
            "memory additional instructions: %s",
            _truncate(agent_context.memory_additional_instructions, 220),
        )
    logger.info(
        "agno skills enabled_dir=%s loaded=%s",
        agent_context.skills_dir or "disabled",
        ", ".join(agent_context.skill_names) if agent_context.skill_names else "none",
    )
    logger.info(
        "audio transcription enabled whisper_model=%s whisper_language=%s audio_temp_dir=%s prefer_memory_language=%s audio_add_history_to_context=%s",
        os.getenv("WHISPER_MODEL", "base"),
        os.getenv("WHISPER_LANGUAGE", "auto"),
        _get_effective_temp_dir("AUDIO_TEMP_DIR"),
        _bool_env("WHISPER_USE_MEMORY_LANGUAGE", True),
        _bool_env("AUDIO_ADD_HISTORY_TO_CONTEXT", False),
    )
    logger.info(
        "image preprocessing enabled max_dim=%s jpeg_quality=%s image_temp_dir=%s image_add_history_to_context=%s",
        os.getenv("IMAGE_MAX_DIM_PX", "1000"),
        os.getenv("IMAGE_JPEG_QUALITY", "80"),
        _get_effective_temp_dir("IMAGE_TEMP_DIR"),
        _bool_env("IMAGE_ADD_HISTORY_TO_CONTEXT", False),
    )
    logger.info(
        "image describe tool model=%s max_tokens=%s",
        os.getenv("IMAGE_DESCRIBE_MODEL", os.getenv("LLM_MODEL", "")),
        os.getenv("IMAGE_DESCRIBE_MAX_TOKENS", "220"),
    )
    if os.getenv("APP_ENV", "").lower() != "development" and not os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN"):
        logger.warning(
            "APP_ENV is not development and TELEGRAM_WEBHOOK_SECRET_TOKEN is empty: webhook calls may be rejected"
        )
    await log_telegram_webhook_info()

    global polling_task
    if _get_telegram_mode() == "polling":
        polling_task = asyncio.create_task(run_telegram_polling_loop(), name="telegram-polling")
        logger.info("telegram polling enabled with timeout=%ss", _get_polling_timeout_seconds())
    else:
        logger.info("telegram webhook mode enabled (no getUpdates polling)")


async def shutdown_polling() -> None:
    global polling_task
    if polling_task and not polling_task.done():
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        logger.info("telegram polling task stopped")
    polling_task = None


existing_lifespan = getattr(app.router, "lifespan_context", None)


@asynccontextmanager
async def app_lifespan(_app):
    if existing_lifespan is not None:
        async with existing_lifespan(_app):
            await startup_diagnostics()
            try:
                yield
            finally:
                await shutdown_polling()
        return

    await startup_diagnostics()
    try:
        yield
    finally:
        await shutdown_polling()


app.router.lifespan_context = app_lifespan


if __name__ == "__main__":
    reload_enabled = _bool_env("RELOAD", False)
    agent_os.serve(app="telegram_bot:app", port=_get_port(), reload=reload_enabled)
