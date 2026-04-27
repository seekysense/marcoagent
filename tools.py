import base64
import json
import os
from contextvars import ContextVar, Token
from typing import Any

import httpx
from agno.tools import tool


# PROMPT TOOL (modifica qui quando vuoi cambiare il criterio di attivazione):
# Usa questo tool SOLO quando l'utente vuole farsi approvare:
# - acquisto di materiale
# - sostituzione di materiale
# - servizi a pagamento
SHAREPOINT_APPROVAL_TOOL_PROMPT = (
    "Usa questo tool solo per richieste di approvazione acquisti/sostituzioni/servizi a pagamento. "
    "Se la richiesta fa riferimento a una foto inviata, usa prima il tool describe_image per estrarre elementi salienti. "
    "Prima di chiamarlo raccogli titolo, descrizione e contesto. "
    "Nel campo context_summary inserisci SEMPRE anche un suggerimento operativo sintetico, generato da te, "
    "su come portare avanti il task (prossimo passo pratico), includendo quando presente i dettagli emersi da describe_image. "
    "Se i dettagli sono incompleti chiedi chiarimenti all'utente. "
    "Dopo l'esecuzione rispondi in modo minimale: solo 'Esito' e, se disponibile, 'Titolo'."
)

DESCRIBE_IMAGE_TOOL_PROMPT = (
    "Usa questo tool quando l'utente fa riferimento a una foto inviata in chat. "
    "Restituisci una descrizione breve orientata all'identificazione oggetto: componente, loghi/marchi, sigle/codici, "
    "stato/danno e dettagli utili per acquisto o sostituzione."
)

AMAZON_PRODUCT_FINDER_TOOL_PROMPT = (
    "Usa questo tool quando l'utente chiede di trovare prodotti su Amazon o alternative da acquistare. "
    "Puoi usarlo anche dopo describe_image quando serve identificare un ricambio/prodotto compatibile. "
    "Questo tool esegue una ricerca diretta su Amazon.it, estrae i risultati HTML e li sintetizza via LLM."
)

_CURRENT_RUN_IMAGE: ContextVar[dict[str, Any] | None] = ContextVar("current_run_image", default=None)
_CURRENT_REQUESTER_EMAIL: ContextVar[str | None] = ContextVar("current_requester_email", default=None)
_CURRENT_RUN_ATTACHMENT: ContextVar[dict[str, Any] | None] = ContextVar("current_run_attachment", default=None)


def _require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if value:
        return value
    raise RuntimeError(f"Missing required environment variable: {name}")


def _get_timeout_seconds() -> float:
    raw = (os.getenv("SHAREPOINT_TASK_TIMEOUT_SECONDS") or "20").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 20.0
    return max(1.0, min(120.0, value))


def _get_int_env(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def _get_float_env(name: str, default: float, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def _normalize_openai_base_url(endpoint: str) -> str:
    raw = endpoint.strip().rstrip("/")
    if not raw:
        raise RuntimeError("Missing required environment variable: LLM_ENDPOINT")
    return raw if raw.endswith("/v1") else f"{raw}/v1"


def _extract_chat_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _sanitize_description_text(text: str) -> str:
    clean = (text or "").strip()
    if not clean:
        return ""

    # Evita leak di payload raw JSON o blocchi tecnici nel testo finale.
    if clean.startswith("{") and clean.endswith("}"):
        return ""
    if "\"choices\"" in clean and "\"message\"" in clean:
        return ""

    # Mantieni output breve e leggibile.
    clean = clean.replace("\r", "")
    lines = [line.strip() for line in clean.split("\n") if line.strip()]
    if len(lines) > 5:
        lines = lines[:5]
    return "\n".join(lines).strip()


def _extract_created_title(payload: Any, fallback_title: str) -> str:
    if isinstance(payload, dict):
        title_keys = ("Titolo", "Title", "title")
        for key in title_keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        fields = payload.get("fields")
        if isinstance(fields, dict):
            for key in title_keys:
                value = fields.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return _extract_created_title(first, fallback_title)

    return fallback_title


def _format_amazon_products_fallback(query: str, products: list[dict[str, str]]) -> str:
    if not products:
        return "Nessun prodotto Amazon trovato."

    out: list[str] = [f'Prodotti Amazon trovati per "{query}":']
    for idx, product in enumerate(products[:3], start=1):
        out.append(
            (
                f"{idx}. {product.get('title', 'Prodotto')}\n"
                f"ASIN: {product.get('asin', '-')}\n"
                f"Prezzo: {product.get('price', '-')}\n"
                f"Valutazione: {product.get('rating', '-')}\n"
                f"Link: {product.get('url', '-')}"
            )
        )
    return "\n\n".join(out)


def _search_amazon_direct(keyword: str) -> tuple[list[dict[str, str]], str | None]:
    try:
        import requests
        from bs4 import BeautifulSoup  # type: ignore
        from urllib.parse import quote_plus
    except Exception:
        return [], "dipendenze mancanti (installa requests e beautifulsoup4)"

    query = (keyword or "").strip()
    if not query:
        return [], "query vuota"

    url = f"https://www.amazon.it/s?k={quote_plus(query)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
    }

    timeout_seconds = _get_float_env("AMAZON_SEARCH_TIMEOUT_SECONDS", 20.0, 5.0, 120.0)
    max_results = _get_int_env("AMAZON_SEARCH_MAX_RESULTS", 5, 1, 12)
    affiliate_id = (os.getenv("AMAZON_AFFILIATE_ID") or "").strip()

    try:
        response = requests.get(url, headers=headers, timeout=timeout_seconds)
        if response.status_code == 503:
            return [], "Amazon ha richiesto CAPTCHA (HTTP 503)"
        response.raise_for_status()
    except Exception as exc:
        return [], f"errore HTTP Amazon: {exc}"

    raw_html = response.text or ""
    if "captcha" in raw_html.lower() or "validatecaptcha" in raw_html.lower():
        return [], "Amazon ha richiesto CAPTCHA"

    soup = BeautifulSoup(raw_html, "html.parser")
    blocks = soup.find_all("div", {"data-component-type": "s-search-result"})

    products: list[dict[str, str]] = []
    for item in blocks:
        asin = str(item.get("data-asin") or "").strip()
        if not asin:
            continue

        title_elem = item.find("h2")
        title = title_elem.get_text(" ", strip=True) if title_elem else ""
        if not title:
            continue

        price = "Prezzo non disponibile"
        price_elem = item.find("span", {"class": "a-price"})
        if price_elem:
            offscreen = price_elem.find("span", {"class": "a-offscreen"})
            if offscreen:
                parsed_price = offscreen.get_text(" ", strip=True)
                if parsed_price:
                    price = parsed_price

        rating = "Nessuna valutazione"
        rating_elem = item.find("span", {"class": "a-icon-alt"})
        if rating_elem:
            parsed_rating = rating_elem.get_text(" ", strip=True)
            if parsed_rating:
                rating = parsed_rating

        url = f"https://www.amazon.it/dp/{asin}"
        if affiliate_id and "YOUR-AFFILIATE-ID" not in affiliate_id:
            url = f"{url}?tag={affiliate_id}"

        products.append(
            {
                "asin": asin,
                "title": title,
                "price": price,
                "rating": rating,
                "url": url,
            }
        )
        if len(products) >= max_results:
            break

    if not products:
        return [], "nessun risultato parsabile trovato"
    return products, None


def _summarize_amazon_products_with_llm(query: str, products: list[dict[str, str]]) -> str:
    if not products:
        return "Nessun prodotto Amazon trovato."

    try:
        llm_endpoint = _require_env("LLM_ENDPOINT")
        llm_api_key = _require_env("LLM_APIKEY")
        llm_model = (os.getenv("AMAZON_FINDER_LLM_MODEL") or os.getenv("LLM_MODEL") or "").strip()
        if not llm_model:
            return _format_amazon_products_fallback(query, products)

        max_tokens = _get_int_env("AMAZON_FINDER_LLM_MAX_TOKENS", 320, 120, 1200)
        timeout_seconds = _get_float_env("AMAZON_FINDER_LLM_TIMEOUT_SECONDS", 25.0, 5.0, 120.0)
        base_url = _normalize_openai_base_url(llm_endpoint)

        system_prompt = (
            "Sei un assistente acquisti tecnico. Ricevi risultati scraping Amazon.it gia' estratti in JSON. "
            "Restituisci una risposta in italiano con massimo 3 prodotti pertinenti alla query. "
            "Per ogni prodotto: Titolo, ASIN, Prezzo, Valutazione, Link. "
            "Non inventare dati. Se i risultati non sono pertinenti, dillo esplicitamente."
        )
        user_prompt = (
            f"Query utente: {query}\n"
            "Risultati estratti:\n"
            f"{json.dumps(products, ensure_ascii=False)}"
        )

        payload = {
            "model": llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception:
        return _format_amazon_products_fallback(query, products)

    if response.status_code < 200 or response.status_code >= 300:
        return _format_amazon_products_fallback(query, products)

    parsed: Any = None
    try:
        parsed = response.json()
    except Exception:
        parsed = None

    text = _extract_chat_text(parsed).strip()
    if not text:
        return _format_amazon_products_fallback(query, products)
    if len(text) > 3000:
        return f"{text[:2997]}..."
    return text


def _find_amazon_product_impl(query: str) -> str:
    query_clean = (query or "").strip()
    if not query_clean:
        return "Ricerca Amazon non disponibile: query vuota."

    products, error = _search_amazon_direct(query_clean)
    if not products:
        if error:
            return f"Ricerca Amazon non disponibile: {error}."
        return "Nessun prodotto Amazon trovato."
    return _summarize_amazon_products_with_llm(query_clean, products)


def set_current_run_image_for_tools(
    image_bytes: bytes,
    *,
    mime_type: str = "image/jpeg",
    local_path: str | None = None,
    telegram_file_path: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> Token:
    data = {
        "image_bytes": image_bytes,
        "mime_type": mime_type,
        "local_path": local_path,
        "telegram_file_path": telegram_file_path,
        "user_id": user_id,
        "session_id": session_id,
    }
    return _CURRENT_RUN_IMAGE.set(data)


def reset_current_run_image_for_tools(token: Token) -> None:
    _CURRENT_RUN_IMAGE.reset(token)


def set_current_requester_email(email: str | None) -> Token:
    return _CURRENT_REQUESTER_EMAIL.set(email)


def reset_current_requester_email(token: Token) -> None:
    _CURRENT_REQUESTER_EMAIL.reset(token)


def set_current_run_attachment_for_tools(
    file_bytes: bytes,
    filename: str,
    mime_type: str = "application/octet-stream",
) -> Token:
    return _CURRENT_RUN_ATTACHMENT.set(
        {"file_bytes": file_bytes, "filename": filename, "mime_type": mime_type}
    )


def reset_current_run_attachment_for_tools(token: Token) -> None:
    _CURRENT_RUN_ATTACHMENT.reset(token)


@tool(
    name="describe_image",
    description=(
        "Descrive brevemente la foto corrente evidenziando elementi salienti per identificare oggetti e componenti."
    ),
    instructions=DESCRIBE_IMAGE_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def describe_image(focus: str | None = None) -> str:
    image_context = _CURRENT_RUN_IMAGE.get()
    if not image_context or not image_context.get("image_bytes"):
        return "Descrizione immagine non disponibile."

    image_bytes = image_context["image_bytes"]
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        return "Descrizione immagine non disponibile."

    try:
        llm_endpoint = _require_env("LLM_ENDPOINT")
        llm_api_key = _require_env("LLM_APIKEY")
        llm_model = (os.getenv("IMAGE_DESCRIBE_MODEL") or os.getenv("LLM_MODEL") or "").strip()
        if not llm_model:
            return "Descrizione immagine non disponibile."

        base_url = _normalize_openai_base_url(llm_endpoint)
        timeout = _get_timeout_seconds()
        max_tokens_raw = (os.getenv("IMAGE_DESCRIBE_MAX_TOKENS") or "220").strip()
        try:
            max_tokens = max(80, min(600, int(max_tokens_raw)))
        except ValueError:
            max_tokens = 220

        system_prompt = (
            "Sei un assistente tecnico. Fornisci una descrizione sintetica in italiano (max 5 righe) "
            "con: oggetto principale, marca/logo, sigle/codici leggibili, stato/usura/danno, "
            "dettagli utili per identificazione/sostituzione."
        )
        user_prompt = (os.getenv("IMAGE_DESCRIBE_USER_PROMPT") or "").strip()
        if not user_prompt:
            user_prompt = "Descrivi gli elementi salienti visibili nell'immagine."
        if focus and focus.strip():
            user_prompt = f"{user_prompt}\nFocus richiesto: {focus.strip()}"

        mime_type = str(image_context.get("mime_type") or "image/jpeg")
        image_b64 = base64.b64encode(bytes(image_bytes)).decode("ascii")
        data_url = f"data:{mime_type};base64,{image_b64}"

        payload = {
            "model": llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception:
        return "Descrizione immagine non disponibile."

    if response.status_code < 200 or response.status_code >= 300:
        return "Descrizione immagine non disponibile."

    parsed: Any = None
    try:
        parsed = response.json()
    except Exception:
        parsed = None

    text = _sanitize_description_text(_extract_chat_text(parsed))
    if not text:
        return "Descrizione immagine non disponibile."

    if len(text) > 1000:
        return f"{text[:997]}..."
    return text


@tool(
    name="find_amazon_product",
    description=(
        "Trova prodotti Amazon.it rilevanti con richiesta HTTP diretta, parsing HTML e sintesi tramite LLM."
    ),
    instructions=AMAZON_PRODUCT_FINDER_TOOL_PROMPT,
    add_instructions=True,
    show_result=True,
)
def find_amazon_product(query: str) -> str:
    return _find_amazon_product_impl(query)


@tool(
    name="create_sharepoint_approval_task",
    description=(
        "Crea un task SharePoint per approvazione acquisti, sostituzione materiale o servizi a pagamento."
    ),
    instructions=SHAREPOINT_APPROVAL_TOOL_PROMPT,
    add_instructions=True,
    show_result=True,
    stop_after_tool_call=True,
)
def create_sharepoint_approval_task(titolo: str, description: str, context_summary: str) -> str:
    titolo_clean = (titolo or "").strip()
    description_clean = (description or "").strip()
    context_clean = (context_summary or "").strip()

    if not titolo_clean:
        return "Esito: errore"
    if not description_clean:
        return "Esito: errore"
    if not context_clean:
        return "Esito: errore"

    webhook_url = _require_env("SHAREPOINT_TASK_WEBHOOK_URL")
    requester_email = _CURRENT_REQUESTER_EMAIL.get() or _require_env("SHAREPOINT_TASK_REQUESTER_EMAIL")
    approver_email = _require_env("SHAREPOINT_TASK_APPROVER_EMAIL")
    task_status = _require_env("SHAREPOINT_TASK_STATUS")

    payload = {
        "Titolo": titolo_clean,
        "Description": description_clean,
        "Approver": approver_email,
        "TaskStatus": task_status,
        "RequesterEmail": requester_email,
        "ContextSummary": context_clean,
    }

    timeout = _get_timeout_seconds()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(
                webhook_url,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
    except Exception:
        return "Esito: errore"

    response_payload: Any = None
    try:
        response_payload = response.json()
    except Exception:
        response_payload = None

    success = 200 <= response.status_code < 300
    if isinstance(response_payload, dict):
        if response_payload.get("ok") is False or response_payload.get("success") is False:
            success = False

    if success:
        created_title = _extract_created_title(response_payload, titolo_clean)
        return f"Esito: creato\nTitolo: {created_title}"

    return "Esito: errore"


GET_TASK_STATUS_TOOL_PROMPT = (
    "Usa questo tool quando l'utente chiede lo stato delle sue richieste, task o approvazioni. "
    "Recupera le richieste usando l'email dell'utente. "
    "Presenta i risultati in modo chiaro e leggibile nella lingua dell'utente (default italiano), "
    "traducendo i valori di stato e le date in formato comprensibile."
)

_TASK_STATUS_TRANSLATIONS: dict[str, str] = {
    "Pending Approval": "In attesa di approvazione",
    "Approved": "Approvato",
    "Rejected": "Rifiutato",
    "In Progress": "In corso",
    "Completed": "Completato",
    "Cancelled": "Annullato",
    "On Hold": "In sospeso",
}


def _translate_task_status(status: str) -> str:
    return _TASK_STATUS_TRANSLATIONS.get(status, status)


def _format_task_date(iso_date: str) -> str:
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso_date


def _fetch_user_tasks(email: str) -> list[dict[str, Any]] | None:
    """Return raw task list for email, or None on network/config error."""
    email_clean = (email or "").strip()
    if not email_clean:
        return None

    webhook_url = (os.getenv("SHAREPOINT_TASK_STATUS_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return None

    timeout = _get_float_env("SHAREPOINT_TASK_TIMEOUT_SECONDS", 20.0, 1.0, 120.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(webhook_url, params={"email": email_clean})
    except Exception:
        return None

    if response.status_code < 200 or response.status_code >= 300:
        return None

    raw = response.text or ""
    data: Any = None
    try:
        data = response.json()
    except Exception:
        pass

    # Fallback: NDJSON (newline-delimited JSON objects) — n8n may return multiple
    # objects separated by newlines instead of a proper JSON array.
    if data is None:
        items: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json as _json
                obj = _json.loads(line)
                if isinstance(obj, dict):
                    items.append(obj)
                elif isinstance(obj, list):
                    items.extend(i for i in obj if isinstance(i, dict))
            except Exception:
                continue
        return items if items else None

    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _verify_task_owned_by_user(email: str, task_id: int) -> tuple[bool, str]:
    """
    Checks that task_id belongs to the user identified by email.
    Returns (True, "") if found, (False, error_message) otherwise.
    """
    tasks = _fetch_user_tasks(email)
    if tasks is None:
        return False, "Impossibile verificare il task: errore di comunicazione con il servizio."
    for task in tasks:
        try:
            if int(task.get("ID", -1)) == task_id:
                return True, ""
        except (TypeError, ValueError):
            continue
    return False, f"Task {task_id} non trovato tra le tue richieste."


def _get_task_status_impl(email: str) -> str:
    email_clean = (email or "").strip()
    if not email_clean:
        return "Email non disponibile per recuperare le richieste."

    if not (os.getenv("SHAREPOINT_TASK_STATUS_WEBHOOK_URL") or "").strip():
        return "Servizio stato richieste non configurato."

    items = _fetch_user_tasks(email_clean)
    if items is None:
        return "Errore durante il recupero delle richieste. Riprova."

    if not items:
        return "Nessuna richiesta trovata per questo utente."

    lines: list[str] = [f"Richieste trovate: {len(items)}\n"]
    for item in items:
        task_id = item.get("ID", "-")
        title = item.get("Title") or item.get("Titolo") or "-"
        status_raw = str(item.get("TaskStatus") or "-")
        status = _translate_task_status(status_raw)
        modified_raw = str(item.get("Modified") or "")
        modified = _format_task_date(modified_raw) if modified_raw else "-"
        lines.append(f"• [{task_id}] {title}\n  Stato: {status}\n  Ultimo aggiornamento: {modified}")

    return "\n".join(lines)


@tool(
    name="get_task_status",
    description="Recupera lo stato delle richieste/task dell'utente corrente.",
    instructions=GET_TASK_STATUS_TOOL_PROMPT,
    add_instructions=True,
    show_result=True,
    stop_after_tool_call=True,
)
def get_task_status() -> str:
    email = _CURRENT_REQUESTER_EMAIL.get() or (os.getenv("SHAREPOINT_TASK_REQUESTER_EMAIL") or "").strip()
    return _get_task_status_impl(email)


ADD_TASK_ATTACHMENT_TOOL_PROMPT = (
    "Usa questo tool quando l'utente vuole aggiungere un allegato a un task specifico, "
    "oppure quando invia un file (documento, immagine) insieme a una richiesta che fa riferimento a un task ID. "
    "Chiedi l'ID del task se non è presente nel contesto della conversazione."
)


@tool(
    name="add_task_attachment",
    description="Aggiunge un allegato (file o immagine) a un task identificato dal suo ID.",
    instructions=ADD_TASK_ATTACHMENT_TOOL_PROMPT,
    add_instructions=True,
    show_result=True,
    stop_after_tool_call=True,
)
def add_task_attachment(task_id: int) -> str:
    attachment = _CURRENT_RUN_ATTACHMENT.get()
    if not attachment or not attachment.get("file_bytes"):
        return "Nessun allegato disponibile. Invia prima il file da allegare."

    email = _CURRENT_REQUESTER_EMAIL.get() or (os.getenv("SHAREPOINT_TASK_REQUESTER_EMAIL") or "").strip()
    ok, err = _verify_task_owned_by_user(email, task_id)
    if not ok:
        return err

    webhook_url = (os.getenv("SHAREPOINT_TASK_ATTACHMENT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return "Servizio allegati non configurato."

    file_bytes: bytes = attachment["file_bytes"]
    filename: str = attachment.get("filename") or "allegato"
    mime_type: str = attachment.get("mime_type") or "application/octet-stream"
    timeout = _get_float_env("SHAREPOINT_TASK_TIMEOUT_SECONDS", 20.0, 1.0, 120.0)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(
                webhook_url,
                data={"id": str(task_id)},
                files={"file": (filename, file_bytes, mime_type)},
            )
    except Exception:
        return "Errore durante l'invio dell'allegato. Riprova."

    try:
        result = response.json()
    except Exception:
        result = {}

    if 200 <= response.status_code < 300 and result.get("result") == "ok":
        return f"Allegato '{filename}' aggiunto con successo al task {task_id}."

    return f"Errore durante l'aggiunta dell'allegato al task {task_id}."


CASTELLETTO_API_ANALYZE_TOOL_PROMPT = (
    "Usa questo tool SOLO per rispondere a domande sulla situazione attuale nell'hotel The Castelletto.\n"
    "Scegli camera_id e action_id adeguati alla domanda:\n\n"
    "TELECAMERE DISPONIBILI:\n"
    "  kitchen     → cucina (colazione, pasti, personale)\n"
    "  entrance    → ingresso principale\n"
    "  lobby       → lobby/reception\n"
    "  parking_01  → parcheggio esterno\n"
    "  patio       → patio/area esterna\n\n"
    "AZIONI DISPONIBILI e campi JSON ritornati:\n"
    "  people_count                  → persone presenti; {count:int, confidence:float, describe:str}\n"
    "  vehicle_count                 → veicoli presenti; {count:int, confidence:float, describe:str}\n"
    "  patio_check                   → stato oggetti patio; {objects_found:[str], describe:str}\n"
    "  cleaning_detection            → pulizie interne; {cleaning_detected:bool, confidence:float, items_found:[str], describe:str}\n"
    "  outdoor_maintenance_detection → manutenzione/pulizie esterne; {cleaning_detected:bool, confidence:float, items_found:[str], describe:str}\n"
    "  buffet_setup_detection        → allestimento buffet/colazione; {buffet_detected:bool, confidence:float, items_found:[str], describe:str}\n\n"
    "ESEMPI DI MAPPING:\n"
    "  'c'è qualcuno a fare colazione?'  → camera=kitchen, action=buffet_setup_detection\n"
    "  'quante persone in cucina?'       → camera=kitchen, action=people_count\n"
    "  'parcheggio vuoto?' / 'ci sono auto?'   → camera=parking_01, action=vehicle_count\n"
    "  'qualcuno sta facendo le pulizie?' → camera=lobby (o kitchen), action=cleaning_detection\n"
    "  'pulizie esterne / patio'          → camera=patio, action=outdoor_maintenance_detection\n"
    "  'com'è il patio?'                  → camera=patio, action=patio_check\n"
    "  'qualcuno in reception/lobby?'     → camera=lobby, action=people_count\n\n"
    "COME INTERPRETARE IL RISULTATO (JSON in inglese → rispondi in italiano naturale):\n"
    "  - confidence > 0.7 = alta certezza; 0.4-0.7 = media; < 0.4 = bassa certezza\n"
    "  - cleaning_detected=false o buffet_detected=false → attività NON rilevata\n"
    "  - count=0 → area vuota, nessuno presente\n"
    "  - items_found → elenca gli elementi in italiano se utili al contesto\n"
    "  - describe → usalo per arricchire la risposta con dettagli visivi\n"
    "  - Indica sempre la telecamera usata (es. 'dalla telecamera della cucina...')"
)

_CASTELLETTO_CAMERA_LABELS: dict[str, str] = {
    "kitchen": "cucina",
    "entrance": "ingresso",
    "lobby": "lobby/reception",
    "parking_01": "parcheggio",
    "patio": "patio",
}


def _castelletto_analyze_impl(camera_id: str, action_id: str) -> str:
    camera_id = (camera_id or "").strip()
    action_id = (action_id or "").strip()
    if not camera_id or not action_id:
        return "Errore: camera_id e action_id sono obbligatori."

    base_url = (os.getenv("CASTELLETTO_API_BASE_URL") or "http://10.40.65.53:8001").rstrip("/")
    api_key = (os.getenv("CASTELLETTO_API_KEY") or "").strip()
    if not api_key:
        return "Servizio telecamere The Castelletto non configurato (CASTELLETTO_API_KEY mancante)."

    timeout = _get_float_env("CASTELLETTO_API_TIMEOUT_SECONDS", 30.0, 5.0, 120.0)
    url = f"{base_url}/analyze/{camera_id}/{action_id}"

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(
                url,
                headers={"X-API-Key": api_key, "accept": "application/json"},
            )
    except Exception as exc:
        return f"Errore di connessione al servizio telecamere: {exc}"

    if response.status_code == 404:
        return f"Telecamera '{camera_id}' o azione '{action_id}' non trovata."
    if response.status_code < 200 or response.status_code >= 300:
        return f"Errore del servizio telecamere (HTTP {response.status_code})."

    try:
        data: Any = response.json()
    except Exception:
        return "Risposta non valida dal servizio telecamere."

    # Result may be wrapped in a 'result' key or returned directly
    result: Any = data.get("result", data) if isinstance(data, dict) else data

    # Sometimes AI APIs embed JSON as a string inside the result
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            pass

    camera_label = _CASTELLETTO_CAMERA_LABELS.get(camera_id, camera_id)
    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    return (
        f"Telecamera: {camera_label} ({camera_id})\n"
        f"Analisi: {action_id}\n"
        f"Risultato:\n{result_json}"
    )


@tool(
    name="castelletto_camera_analyze",
    description=(
        "Analizza in tempo reale una telecamera dell'hotel The Castelletto per rispondere "
        "a domande su presenze, attività, parcheggio, colazione, pulizie e stato degli spazi."
    ),
    instructions=CASTELLETTO_API_ANALYZE_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def castelletto_camera_analyze(camera_id: str, action_id: str) -> str:
    return _castelletto_analyze_impl(camera_id, action_id)


def get_agent_tools() -> list[Any]:
    return [
        describe_image,
        find_amazon_product,
        create_sharepoint_approval_task,
        get_task_status,
        add_task_attachment,
        castelletto_camera_analyze,
    ]
