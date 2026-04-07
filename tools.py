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
    requester_email = _require_env("SHAREPOINT_TASK_REQUESTER_EMAIL")
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


def get_agent_tools() -> list[Any]:
    return [describe_image, find_amazon_product, create_sharepoint_approval_task]
