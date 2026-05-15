import asyncio
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv

from agent import AgentContext, build_agent_context, require_env
from tools import set_current_caller_telegram_id
from agno.media import Image
from agno.os.app import AgentOS
from agno.os.interfaces.telegram import Telegram
from storage_data import get_user_by_telegram_id, register_user_from_contact
from utilities import (
    download_telegram_document,
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


class _AgnoSchedulerPollFilter(logging.Filter):
    """Suppresses the transient 404 noise produced by Agno's ScheduleExecutor.

    When a scheduled run is in-flight, the executor polls GET /runs/{run_id}
    immediately (no sleep on 404) and the AgentOS logs a WARNING + ERROR for
    each poll that hits while the agent is still running. These messages are
    expected and not actionable — the run completes correctly on the next poll.
    """

    _PATTERNS = (
        re.compile(r"RunOutput .+ not found in Session"),
        re.compile(r"HTTP exception: 404 Run not found"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p.search(msg) for p in self._PATTERNS)


def _configure_logging() -> logging.Logger:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")

    logger_instance = logging.getLogger("telegram_bot")
    logger_instance.setLevel(level)
    if not logger_instance.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger_instance.addHandler(handler)
    logger_instance.propagate = False

    # Suppress transient 404 polling noise from Agno's scheduler executor
    agno_logger = logging.getLogger("agno")
    agno_logger.addFilter(_AgnoSchedulerPollFilter())

    tools_logger = logging.getLogger("marco.tools")
    tools_logger.setLevel(level)
    if not tools_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        tools_logger.addHandler(handler)
    tools_logger.propagate = False

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


def _extract_document(message: dict[str, Any]) -> tuple[str | None, str, str]:
    """Returns (file_id, filename, mime_type) for document messages, or (None, '', '') if absent."""
    doc = message.get("document")
    if not isinstance(doc, dict) or not doc.get("file_id"):
        return None, "", ""
    file_id = str(doc["file_id"])
    filename = str(doc.get("file_name") or "allegato").strip()
    mime_type = str(doc.get("mime_type") or "application/octet-stream").strip()
    return file_id, filename, mime_type


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


def _normalize_phone(phone: str) -> str:
    phone = phone.strip()
    if phone.startswith("+"):
        return phone
    if phone.startswith("00"):
        return "+" + phone[2:]
    # Number already contains Italian country code but without leading +
    if phone.startswith("39") and len(phone) >= 11:
        return "+" + phone
    return "+39" + phone


async def _request_contact_sharing(
    token: str,
    chat_id: str | int,
    message_thread_id: str | int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": (
            "Per utilizzare questo bot devi essere registrato. "
            "Condividi il tuo numero di telefono premendo il pulsante qui sotto."
        ),
        "reply_markup": {
            "keyboard": [[{"text": "📱 Condividi il tuo numero", "request_contact": True}]],
            "one_time_keyboard": True,
            "resize_keyboard": True,
        },
    }
    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)
    async with httpx.AsyncClient(timeout=20.0) as client:
        await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)


async def _send_message_remove_keyboard(
    token: str,
    chat_id: str | int,
    text: str,
    message_thread_id: str | int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"remove_keyboard": True},
    }
    if message_thread_id is not None:
        payload["message_thread_id"] = int(message_thread_id)
    async with httpx.AsyncClient(timeout=20.0) as client:
        await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)


load_dotenv()

# Lista di frasi di feedback random
FEEDBACK_PHRASES = [
    "Ok, ci lavoro!",
    "Dammi un attimo, fammi pensare...",
    "Ricevuto! Inizio a lavorarci...",
    "Messaggio preso in carico, elaboro subito.",
    "Capito, sto elaborando la tua richiesta.",
    "Un momento, sto pensando alla risposta.",
    "Ricevuto, procedo con l'elaborazione.",
    "Ok, mi metto al lavoro!",
    "Messaggio ricevuto, inizio l'analisi.",
    "D'accordo, sto elaborando..."
]

def get_random_feedback_phrase() -> str:
    return random.choice(FEEDBACK_PHRASES)

# Telegram webhook secret checks are strict outside development mode.
if not os.getenv("APP_ENV"):
    os.environ["APP_ENV"] = "development"

logger = _configure_logging()
agent_context: AgentContext = build_agent_context()
agent_id = agent_context.agent_id

# Accept the typo already present in the current .env for compatibility.
telegram_token = require_env("TELEGRAM_TOKEN", "TELEGHRAM_BOT_TOKEN")

_scheduler_base_url = f"http://127.0.0.1:{_get_port()}"

agent_os = AgentOS(
    agents=[agent_context.agent],
    interfaces=[
        Telegram(
            agent=agent_context.agent,
            token=telegram_token,
            streaming=_bool_env("TELEGRAM_STREAMING", True),
        )
    ],
    db=agent_context.db,
    scheduler=True,
    scheduler_poll_interval=int(os.getenv("SCHEDULER_POLL_INTERVAL", "60")),
    scheduler_base_url=_scheduler_base_url,
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
                chat_id_for_send = (message_obj.get("chat") or {}).get("id")
                thread_id_for_send = message_obj.get("message_thread_id")
                user_id_for_memory = str(user_id_raw) if user_id_raw is not None else None
                chat_id_for_scope = str(chat_id_for_send) if chat_id_for_send is not None else None
                thread_id_for_scope = str(thread_id_for_send) if thread_id_for_send is not None else None
                session_scope = _build_session_scope(agent_id, chat_id_for_scope, thread_id_for_scope)
                existing_text = (message_obj.get("text") or message_obj.get("caption") or "").strip()

                # --- Contact sharing ---
                contact = message_obj.get("contact")
                if isinstance(contact, dict) and user_id_for_memory and chat_id_for_send is not None:
                    phone_raw = str(contact.get("phone_number") or "").strip()
                    phone = _normalize_phone(phone_raw) if phone_raw else ""
                    first_name = str(contact.get("first_name") or "").strip()
                    last_name = str(contact.get("last_name") or "").strip()
                    full_name = " ".join(filter(None, [first_name, last_name])) or "Utente"
                    if phone:
                        register_user_from_contact(
                            agent_context.db_file, user_id_for_memory, phone, full_name
                        )
                        logger.info(
                            "user registered from contact telegram_id=%s phone=%s",
                            user_id_for_memory,
                            phone,
                        )
                        await _send_message_remove_keyboard(
                            token=telegram_token,
                            chat_id=chat_id_for_send,
                            text=f"Benvenuto {first_name or 'utente'}! Registrazione completata. Ora puoi usarmi.",
                            message_thread_id=thread_id_for_send,
                        )
                    else:
                        await _send_telegram_message(
                            token=telegram_token,
                            chat_id=chat_id_for_send,
                            text="Non ho ricevuto un numero di telefono valido. Riprova.",
                            message_thread_id=thread_id_for_send,
                        )
                    continue

                # --- User recognition ---
                user_record: dict | None = None
                if user_id_for_memory and chat_id_for_send is not None:
                    user_record = get_user_by_telegram_id(agent_context.db_file, user_id_for_memory)
                    if user_record is None:
                        logger.info(
                            "unknown user telegram_id=%s, requesting contact sharing",
                            user_id_for_memory,
                        )
                        await _request_contact_sharing(
                            token=telegram_token,
                            chat_id=chat_id_for_send,
                            message_thread_id=thread_id_for_send,
                        )
                        continue

                # --- Skip /start ---
                if existing_text.lower().startswith("/start"):
                    continue

                # --- Immediate feedback ---
                if chat_id_for_send is not None:
                    await _send_telegram_message(
                        token=telegram_token,
                        chat_id=chat_id_for_send,
                        text=get_random_feedback_phrase(),
                        message_thread_id=thread_id_for_send,
                    )

                try:
                    # --- Document ---
                    doc_file_id, doc_filename, doc_mime_type = _extract_document(message_obj)
                    if doc_file_id:
                        try:
                            doc_result = await download_telegram_document(
                                token=telegram_token,
                                file_id=doc_file_id,
                                filename=doc_filename,
                                update_id=update_id if isinstance(update_id, int) else None,
                                logger=logger,
                            )
                            logger.info(
                                "document downloaded update_id=%s filename=%s size=%s",
                                update_id,
                                doc_result.filename,
                                len(doc_result.file_bytes),
                            )
                            agent_input = (
                                f"[Allegato: {doc_result.filename}] {existing_text}"
                                if existing_text
                                else f"[Allegato: {doc_result.filename}]"
                            )
                            run_kwargs: dict[str, Any] = {
                                "stream": False,
                                "user_id": user_id_for_memory,
                            }
                            if session_scope:
                                run_kwargs["session_id"] = session_scope
                            set_current_caller_telegram_id(user_id_for_memory)
                            logger.info("[LLM] arun start  update_id=%s user_id=%s kind=document input=%s", update_id, user_id_for_memory, _truncate(agent_input))
                            _arun_t0 = time.perf_counter()
                            run_output = await agent_context.agent.arun(agent_input, **run_kwargs)
                            logger.info("[LLM] arun done   update_id=%s  %.1fs  status=%s", update_id, time.perf_counter() - _arun_t0, getattr(run_output, "status", "?"))
                            run_content = str(getattr(run_output, "content", "") or "").strip()
                            if chat_id_for_send is not None:
                                await _send_telegram_message(
                                    token=telegram_token,
                                    chat_id=chat_id_for_send,
                                    text=run_content or "Operazione completata.",
                                    message_thread_id=thread_id_for_send,
                                )
                        except Exception:
                            logger.exception("document pipeline failed update_id=%s", update_id)
                            if chat_id_for_send is not None:
                                await _send_telegram_message(
                                    token=telegram_token,
                                    chat_id=chat_id_for_send,
                                    text="Errore durante la gestione del documento. Riprova.",
                                    message_thread_id=thread_id_for_send,
                                )
                        continue

                    # --- Photo ---
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
                            if session_scope:
                                run_kwargs["session_id"] = session_scope
                            set_current_caller_telegram_id(user_id_for_memory)
                            logger.info("[LLM] arun start  update_id=%s user_id=%s kind=image prompt=%s", update_id, user_id_for_memory, _truncate(image_prompt))
                            _arun_t0 = time.perf_counter()
                            run_output = await agent_context.agent.arun(image_prompt, **run_kwargs)
                            run_status = str(getattr(run_output, "status", ""))
                            run_content = str(getattr(run_output, "content", "") or "").strip()
                            logger.info(
                                "[LLM] arun done   update_id=%s  %.1fs  status=%s  has_content=%s",
                                update_id,
                                time.perf_counter() - _arun_t0,
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

                    # --- Audio ---
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
                            agent_input = (
                                f"{existing_text}\n\n{quoted_transcript}" if existing_text else quoted_transcript
                            )
                            logger.info(
                                "[LLM] arun start  update_id=%s user_id=%s kind=audio input=%s",
                                update_id,
                                user_id_for_memory,
                                _truncate(agent_input, 220),
                            )
                            run_kwargs: dict[str, Any] = {
                                "stream": False,
                                "user_id": user_id_for_memory,
                                "add_history_to_context": _bool_env("AUDIO_ADD_HISTORY_TO_CONTEXT", False),
                            }
                            if session_scope:
                                run_kwargs["session_id"] = session_scope
                            set_current_caller_telegram_id(user_id_for_memory)
                            _arun_t0 = time.perf_counter()
                            run_output = await agent_context.agent.arun(agent_input, **run_kwargs)
                            run_status = str(getattr(run_output, "status", ""))
                            run_content = str(getattr(run_output, "content", "") or "").strip()
                            logger.info(
                                "[LLM] arun done   update_id=%s  %.1fs  status=%s  has_content=%s",
                                update_id,
                                time.perf_counter() - _arun_t0,
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
                        except Exception:
                            logger.exception("audio pipeline failed update_id=%s", update_id)
                            if chat_id_for_send is not None:
                                await _send_telegram_message(
                                    token=telegram_token,
                                    chat_id=chat_id_for_send,
                                    text="Errore durante la gestione dell'audio. Riprova con un nuovo vocale.",
                                    message_thread_id=thread_id_for_send,
                                )
                        continue

                    # --- Text (direct run, consistent with audio/image) ---
                    if not existing_text:
                        continue
                    logger.info(
                        "[LLM] arun start  update_id=%s user_id=%s kind=text input=%s",
                        update_id,
                        user_id_for_memory,
                        _truncate(existing_text),
                    )
                    try:
                        run_kwargs: dict[str, Any] = {
                            "stream": False,
                            "user_id": user_id_for_memory,
                        }
                        if session_scope:
                            run_kwargs["session_id"] = session_scope
                        set_current_caller_telegram_id(user_id_for_memory)
                        _arun_t0 = time.perf_counter()
                        run_output = await agent_context.agent.arun(existing_text, **run_kwargs)
                        run_content = str(getattr(run_output, "content", "") or "").strip()
                        logger.info("[LLM] arun done   update_id=%s  %.1fs  has_content=%s", update_id, time.perf_counter() - _arun_t0, bool(run_content))
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
                                    text="Non sono riuscito a generare una risposta. Riprova.",
                                    message_thread_id=thread_id_for_send,
                                )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("text direct-run failed update_id=%s", update_id)
                        if chat_id_for_send is not None:
                            await _send_telegram_message(
                                token=telegram_token,
                                chat_id=chat_id_for_send,
                                text="Si è verificato un errore. Riprova.",
                                message_thread_id=thread_id_for_send,
                            )

                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("update processing failed update_id=%s", update_id)


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
        "scheduler enabled base_url=%s poll_interval=%ss",
        _scheduler_base_url,
        os.getenv("SCHEDULER_POLL_INTERVAL", "60"),
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
