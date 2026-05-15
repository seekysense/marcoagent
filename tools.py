import base64
import json
import logging
import os
import time
from contextvars import ContextVar
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from agno.tools import tool

_tlog = logging.getLogger("marco.tools")

from agno.scheduler.manager import ScheduleManager
from storage_data import (
    create_user_by_phone,
    delete_user_by_phone,
    get_user_by_telegram_id,
    list_users,
)

# ---------------------------------------------------------------------------
# Caller identity — set by the bot before each agent.arun() call
# ---------------------------------------------------------------------------

_current_caller_telegram_id: ContextVar[str | None] = ContextVar(
    "_current_caller_telegram_id", default=None
)


def set_current_caller_telegram_id(telegram_id: str | None) -> None:
    _current_caller_telegram_id.set(telegram_id)


def _db_file() -> str:
    return os.getenv("AGNO_MEMORY_DB_FILE", "memory.sqllite")


def _check_admin() -> str | None:
    """Return None if the current caller is admin, else an error message."""
    caller = (_current_caller_telegram_id.get() or "").strip()
    if not caller:
        return "Impossibile verificare l'identità del richiedente."
    user = get_user_by_telegram_id(_db_file(), caller)
    if user is None or user.get("role") != "admin":
        return "Non hai i permessi per questa operazione."
    return None


def _normalize_phone(phone: str) -> str:
    phone = phone.strip()
    if phone.startswith("+"):
        return phone
    if phone.startswith("00"):
        return "+" + phone[2:]
    if phone.startswith("39") and len(phone) >= 11:
        return "+" + phone
    return "+39" + phone

_ROME_TZ = ZoneInfo("Europe/Rome")
_UTC_TZ = ZoneInfo("UTC")


def _parse_at_to_utc(at: str) -> str:
    """Convert a datetime string to UTC ISO 8601. Naive datetimes are assumed to be Europe/Rome."""
    at = at.strip().replace(" ", "T")
    if at.endswith("Z"):
        at = at[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(at)
    except ValueError:
        return at  # unparseable: pass through and let the API reject it
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ROME_TZ)
    return dt.astimezone(_UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_to_rome(utc_str: str) -> str:
    """Convert a UTC ISO 8601 string to Europe/Rome for display."""
    try:
        s = utc_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC_TZ)
        return dt.astimezone(_ROME_TZ).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return utc_str


def _get_float_env(name: str, default: float, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


# ---------------------------------------------------------------------------
# Castelletto: analyze (single frame)
# ---------------------------------------------------------------------------

CASTELLETTO_API_ANALYZE_TOOL_PROMPT = (
    "Usa questo tool SOLO per rispondere a domande sulla situazione attuale nell'hotel The Castelletto.\n"
    "Scegli camera_id e action_id adeguati alla domanda:\n\n"
    "TELECAMERE DISPONIBILI:\n"
    "  tc_kitchen          → cucina (colazione, pasti, personale)\n"
    "  tc_kitchen_cabinet  → armadio/dispensa cucina (Nexocab)\n"
    "  tc_lobby            → lobby/reception\n"
    "  tc_parking_01       → parcheggio esterno\n"
    "  tc_patio            → patio/area esterna\n"
    "  tc_lockers          → armadietti (lockers)\n\n"
    "AZIONI DISPONIBILI e campi JSON ritornati:\n"
    "  people_count                  → persone presenti; {number:int, confidence:float, describe:str}\n"
    "  vehicle_count                 → veicoli presenti; {number:int, confidence:float, describe:str}\n"
    "  patio_check                   → stato oggetti patio; {objects_found:[str], describe:str}\n"
    "  cleaning_detection            → pulizie interne; {cleaning_detected:bool, confidence:float, items_found:[str], describe:str}\n"
    "  outdoor_maintenance_detection → manutenzione/pulizie esterne; {cleaning_detected:bool, confidence:float, items_found:[str], describe:str}\n"
    "  buffet_setup_detection        → allestimento buffet/colazione; {buffet_detected:bool, confidence:float, items_found:[str], describe:str}\n\n"
    "PARAMETRO 'at' (opzionale):\n"
    "  - Lascia vuoto per analisi live (adesso)\n"
    "  - Se l'utente fa riferimento a un orario passato, passa l'ora esattamente come l'ha detta (es. '08:00' o '2026-05-14T08:00:00')\n"
    "  - NON fare conversioni di fuso orario: ci pensa il sistema\n\n"
    "ESEMPI DI MAPPING:\n"
    "  'c'è qualcuno a fare colazione?'        → camera=tc_kitchen, action=buffet_setup_detection, at=''\n"
    "  'c'era colazione alle 8?'               → camera=tc_kitchen, action=buffet_setup_detection, at='<oggi>T08:00:00'\n"
    "  'quante persone in cucina?'             → camera=tc_kitchen, action=people_count, at=''\n"
    "  'parcheggio vuoto?' / 'ci sono auto?'   → camera=tc_parking_01, action=vehicle_count, at=''\n"
    "  'qualcuno sta facendo le pulizie?'      → camera=tc_lobby, action=cleaning_detection, at=''\n"
    "  'pulizie esterne / patio'               → camera=tc_patio, action=outdoor_maintenance_detection, at=''\n"
    "  'com'è il patio?'                       → camera=tc_patio, action=patio_check, at=''\n"
    "  'qualcuno in reception/lobby?'          → camera=tc_lobby, action=people_count, at=''\n\n"
    "  'qualcuno davanti ai lockers?'          → camera=tc_lockers, action=people_count, at=''\n\n"
    "  'c'è un cane in cucina?'                → camera=tc_kitchen, action=dog_detection, at=''\n\n"
    "RISPOSTA ALL'UTENTE:\n"
    "  - Rispondi in italiano colloquiale, come se stessi descrivendo quello che vedi\n"
    "  - NON riportare valori tecnici (confidence, number, boolean, campi JSON, nomi dei campi)\n"
    "  - NON menzionare la telecamera usata, il nome dell'azione o parametri interni\n"
    "  - Usa il contenuto del campo 'describe' per arricchire la risposta con dettagli visivi\n"
    "  - Sii diretto e conciso: una o due frasi bastano"
)

_CASTELLETTO_CAMERA_LABELS: dict[str, str] = {
    "tc_kitchen": "cucina",
    "tc_kitchen_cabinet": "armadio cucina (Nexocab)",
    "tc_lobby": "lobby/reception",
    "tc_parking_01": "parcheggio",
    "tc_patio": "patio",
    "tc_lockers": "armadietti (lockers)",
}


def _castelletto_analyze_impl(camera_id: str, action_id: str, at: str = "") -> str:
    camera_id = (camera_id or "").strip()
    action_id = (action_id or "").strip()
    _tlog.info("[TOOL] castelletto_analyze  camera=%-20s action=%-30s at=%r", camera_id, action_id, at or "live")
    _t0 = time.perf_counter()
    if not camera_id or not action_id:
        return "Errore: camera_id e action_id sono obbligatori."

    base_url = (os.getenv("CASTELLETTO_API_BASE_URL") or "").rstrip("/")
    api_key = (os.getenv("CASTELLETTO_API_KEY") or "").strip()
    if not api_key:
        return "Servizio telecamere The Castelletto non configurato (CASTELLETTO_API_KEY mancante)."

    timeout = _get_float_env("CASTELLETTO_API_TIMEOUT_SECONDS", 30.0, 5.0, 120.0)
    url = f"{base_url}/analyze/{camera_id}/{action_id}"
    params: dict[str, str] = {}
    at = (at or "").strip()
    if at:
        params["at"] = _parse_at_to_utc(at)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(
                url,
                headers={"X-API-Key": api_key, "accept": "application/json"},
                params=params,
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

    result: Any = data.get("result", data) if isinstance(data, dict) else data
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            pass

    camera_label = _CASTELLETTO_CAMERA_LABELS.get(camera_id, camera_id)
    result_json = json.dumps(result, ensure_ascii=False, indent=2)
    out = (
        f"Telecamera: {camera_label} ({camera_id})\n"
        f"Analisi: {action_id}\n"
        f"Risultato:\n{result_json}"
    )
    _tlog.info("[DONE] castelletto_analyze  %.1fs | %s", time.perf_counter() - _t0, result_json[:120].replace("\n", " "))
    return out


@tool(
    name="castelletto_camera_analyze",
    description=(
        "Analizza una telecamera dell'hotel The Castelletto per rispondere a domande su presenze, attività, "
        "parcheggio, colazione, pulizie, stato degli spazi. Supporta analisi live e su registrazioni passate."
    ),
    instructions=CASTELLETTO_API_ANALYZE_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def castelletto_camera_analyze(camera_id: str, action_id: str, at: str = "") -> str:
    return _castelletto_analyze_impl(camera_id, action_id, at)


# ---------------------------------------------------------------------------
# Castelletto: list actions
# ---------------------------------------------------------------------------

CASTELLETTO_LIST_ACTIONS_TOOL_PROMPT = (
    "Usa questo tool per ottenere la lista aggiornata delle azioni disponibili sulle telecamere The Castelletto. "
    "Utile per scoprire quali analisi istantanee (single-frame) sono possibili prima di chiamare castelletto_camera_analyze. "
    "Non richiede parametri."
)


def _castelletto_list_actions_impl() -> str:
    _tlog.info("[TOOL] castelletto_list_actions")
    _t0 = time.perf_counter()
    base_url = (os.getenv("CASTELLETTO_API_BASE_URL") or "").rstrip("/")
    api_key = (os.getenv("CASTELLETTO_API_KEY") or "").strip()
    if not api_key:
        return "Servizio telecamere The Castelletto non configurato (CASTELLETTO_API_KEY mancante)."

    timeout = _get_float_env("CASTELLETTO_API_TIMEOUT_SECONDS", 30.0, 5.0, 120.0)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(
                f"{base_url}/actions",
                headers={"X-API-Key": api_key, "accept": "application/json"},
            )
    except Exception as exc:
        return f"Errore di connessione al servizio telecamere: {exc}"

    if response.status_code < 200 or response.status_code >= 300:
        return f"Errore del servizio telecamere (HTTP {response.status_code})."

    try:
        actions: Any = response.json()
    except Exception:
        return "Risposta non valida dal servizio telecamere."

    if not isinstance(actions, list) or not actions:
        return "Nessuna azione disponibile."

    lines = ["Azioni disponibili:"]
    for a in actions:
        action_id = a.get("id", "?")
        label = a.get("label", action_id)
        lines.append(f"  {action_id} → {label}")
    out = "\n".join(lines)
    _tlog.info("[DONE] castelletto_list_actions  %.1fs | %d azioni", time.perf_counter() - _t0, len(actions))
    return out


@tool(
    name="castelletto_list_actions",
    description="Restituisce la lista delle azioni (analisi single-frame) disponibili sulle telecamere The Castelletto.",
    instructions=CASTELLETTO_LIST_ACTIONS_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def castelletto_list_actions() -> str:
    return _castelletto_list_actions_impl()


# ---------------------------------------------------------------------------
# Castelletto: list sequences
# ---------------------------------------------------------------------------

CASTELLETTO_LIST_SEQUENCES_TOOL_PROMPT = (
    "Usa questo tool per ottenere la lista aggiornata delle sequenze disponibili sulle telecamere The Castelletto. "
    "Le sequenze analizzano una finestra temporale di video (live o da registrazione SD) con un pipeline multi-chunk LLM. "
    "Utile per scoprire quali sequenze sono disponibili prima di chiamare castelletto_run_sequence. "
    "Non richiede parametri."
)


def _castelletto_list_sequences_impl() -> str:
    _tlog.info("[TOOL] castelletto_list_sequences")
    _t0 = time.perf_counter()
    base_url = (os.getenv("CASTELLETTO_API_BASE_URL") or "").rstrip("/")
    api_key = (os.getenv("CASTELLETTO_API_KEY") or "").strip()
    if not api_key:
        return "Servizio telecamere The Castelletto non configurato (CASTELLETTO_API_KEY mancante)."

    timeout = _get_float_env("CASTELLETTO_API_TIMEOUT_SECONDS", 30.0, 5.0, 120.0)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(
                f"{base_url}/sequence",
                headers={"X-API-Key": api_key, "accept": "application/json"},
            )
    except Exception as exc:
        return f"Errore di connessione al servizio telecamere: {exc}"

    if response.status_code < 200 or response.status_code >= 300:
        return f"Errore del servizio telecamere (HTTP {response.status_code})."

    try:
        sequences: Any = response.json()
    except Exception:
        return "Risposta non valida dal servizio telecamere."

    if not isinstance(sequences, list) or not sequences:
        return "Nessuna sequenza disponibile."

    lines = ["Sequenze disponibili:"]
    for s in sequences:
        seq_id = s.get("id", "?")
        label = s.get("label", seq_id)
        fps = s.get("fps", "?")
        before = s.get("window_before_s", "?")
        after = s.get("window_after_s", "?")
        lines.append(f"  {seq_id} → {label} (finestra: -{before}s/+{after}s a {fps} fps)")
    out = "\n".join(lines)
    _tlog.info("[DONE] castelletto_list_sequences  %.1fs | %d sequenze", time.perf_counter() - _t0, len(sequences))
    return out


@tool(
    name="castelletto_list_sequences",
    description="Restituisce la lista delle sequenze (analisi video multi-chunk) disponibili sulle telecamere The Castelletto.",
    instructions=CASTELLETTO_LIST_SEQUENCES_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def castelletto_list_sequences() -> str:
    return _castelletto_list_sequences_impl()


# ---------------------------------------------------------------------------
# Castelletto: run sequence
# ---------------------------------------------------------------------------

CASTELLETTO_RUN_SEQUENCE_TOOL_PROMPT = (
    "Usa questo tool per eseguire un'analisi a sequenza (finestra temporale video, pipeline multi-chunk LLM) "
    "su una telecamera The Castelletto.\n\n"
    "TELECAMERE DISPONIBILI:\n"
    "  tc_kitchen          → cucina\n"
    "  tc_kitchen_cabinet  → armadio/dispensa cucina (Nexocab)\n"
    "  tc_lobby            → lobby/reception\n"
    "  tc_parking_01       → parcheggio\n"
    "  tc_patio            → patio/area esterna\n\n"
    "PARAMETRO 'at' (opzionale):\n"
    "  - Lascia vuoto ('') per analisi live (frames catturati in tempo reale)\n"
    "  - Se l'utente fa riferimento a un orario passato, passa l'ora esattamente come l'ha detta (es. '10:30' o '2024-06-01T10:30:00')\n"
    "  - NON fare conversioni di fuso orario: ci pensa il sistema\n\n"
    "ESEMPI DI MAPPING:\n"
    "  'qualcuno ha preso qualcosa dall'armadio Nexocab verso le 10:30?' "
    "→ camera=tc_kitchen_cabinet, sequence=cabinet_access_detection, at='<oggi>T10:30:00'\n"
    "  'analizza l'accesso all'armadio adesso' → camera=tc_kitchen_cabinet, sequence=cabinet_access_detection, at=''\n\n"
    "RISPOSTA ALL'UTENTE:\n"
    "  - Rispondi in italiano colloquiale, come se stessi descrivendo quello che è successo\n"
    "  - NON riportare valori tecnici (verdict, confidence, boolean, campi JSON, nomi dei campi)\n"
    "  - NON menzionare la telecamera, la sequenza o parametri interni\n"
    "  - Per cabinet_access_detection traduci il verdetto: CONFIRMED_ACCESS=accesso confermato, SUSPICIOUS=situazione sospetta, CLEAR=nessun accesso rilevato\n"
    "  - Sii diretto e conciso: una o due frasi bastano"
)


def _castelletto_run_sequence_impl(camera_id: str, sequence_id: str, at: str) -> str:
    camera_id = (camera_id or "").strip()
    sequence_id = (sequence_id or "").strip()
    _tlog.info("[TOOL] castelletto_run_sequence  camera=%-20s sequence=%-30s at=%r", camera_id, sequence_id, at or "live")
    _t0 = time.perf_counter()
    if not camera_id or not sequence_id:
        return "Errore: camera_id e sequence_id sono obbligatori."

    base_url = (os.getenv("CASTELLETTO_API_BASE_URL") or "").rstrip("/")
    api_key = (os.getenv("CASTELLETTO_API_KEY") or "").strip()
    if not api_key:
        return "Servizio telecamere The Castelletto non configurato (CASTELLETTO_API_KEY mancante)."

    timeout = _get_float_env("CASTELLETTO_API_TIMEOUT_SECONDS", 30.0, 5.0, 120.0)
    url = f"{base_url}/sequence/{camera_id}/{sequence_id}"
    params: dict[str, str] = {}
    at = (at or "").strip()
    at_rome_display = ""
    if at:
        at_utc = _parse_at_to_utc(at)
        params["at"] = at_utc
        at_rome_display = _utc_to_rome(at_utc)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.post(
                url,
                headers={"X-API-Key": api_key, "accept": "application/json"},
                params=params,
            )
    except Exception as exc:
        return f"Errore di connessione al servizio telecamere: {exc}"

    if response.status_code == 404:
        return f"Telecamera '{camera_id}' o sequenza '{sequence_id}' non trovata."
    if response.status_code < 200 or response.status_code >= 300:
        return f"Errore del servizio telecamere (HTTP {response.status_code})."

    try:
        data: Any = response.json()
    except Exception:
        return "Risposta non valida dal servizio telecamere."

    camera_label = _CASTELLETTO_CAMERA_LABELS.get(camera_id, camera_id)
    source = data.get("source", "?")
    chunks = data.get("chunks_analyzed", "?")
    final_result = data.get("final_result", {})
    at_display = f" | Riferimento: {at_rome_display} (ora di Roma)" if at_rome_display else ""

    final_json = json.dumps(final_result, ensure_ascii=False, indent=2)
    out = (
        f"Telecamera: {camera_label} ({camera_id})\n"
        f"Sequenza: {sequence_id}\n"
        f"Fonte: {source}{at_display} | Chunk analizzati: {chunks}\n"
        f"Risultato finale:\n{final_json}"
    )
    _tlog.info("[DONE] castelletto_run_sequence  %.1fs | chunks=%s %s", time.perf_counter() - _t0, chunks, final_json[:100].replace("\n", " "))
    return out


@tool(
    name="castelletto_run_sequence",
    description=(
        "Esegue un'analisi a sequenza (finestra temporale video, pipeline multi-chunk LLM) su una telecamera "
        "The Castelletto. Supporta analisi live e su registrazioni storiche da SD card."
    ),
    instructions=CASTELLETTO_RUN_SEQUENCE_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def castelletto_run_sequence(camera_id: str, sequence_id: str, at: str = "") -> str:
    return _castelletto_run_sequence_impl(camera_id, sequence_id, at)


# ---------------------------------------------------------------------------
# PMS (Beddy.io): prenotazioni The Castelletto
# ---------------------------------------------------------------------------

PMS_TOOL_PROMPT = (
    "Usa questo tool per rispondere a domande sulle prenotazioni dell'hotel The Castelletto.\n\n"
    "QUANDO USARLO:\n"
    "  - Arrivi / partenze di oggi, domani, questa settimana\n"
    "  - Chi è attualmente in struttura\n"
    "  - Stato pre-checkin (documento comunicato) per ospiti in arrivo\n"
    "  - Note speciali, animali, trattamenti, camera assegnata\n\n"
    "PARAMETRI DATE (formato YYYY-MM-DD):\n"
    "  - Arrivi: arrival_date_from / arrival_date_to\n"
    "  - Partenze: departure_date_from / departure_date_to\n"
    "  - Ospiti in struttura in un giorno specifico: stay_date\n"
    "  - Se l'utente non specifica un periodo, chiedi 'per quale giorno o periodo?' "
    "prima di chiamare il tool\n\n"
    "RISPOSTA ALL'UTENTE — REGOLE DI PRIVACY:\n"
    "  - Rispondi in italiano colloquiale\n"
    "  - NON mostrare mai: email, telefono, numero documento, pin checkin, link checkin\n"
    "  - I dati personali degli ospiti (email, telefono) mostrarli SOLO se l'utente li chiede esplicitamente\n"
    "  - Mostra nome e cognome degli ospiti solo quando rilevante per la domanda\n"
    "  - Evidenzia sempre lo stato pre-checkin (documento comunicato: sì/no) tra gli arrivi\n"
    "  - Per le note, usa il contenuto in modo naturale senza mostrare il tipo tecnico\n"
    "  - Sii conciso: dai le informazioni operative utili senza elencare tutto il JSON\n"
    "  - NON mostrare ID numerici delle prenotazioni"
)


def _pms_auth_header() -> str:
    user = (os.getenv("PMS_BASIC_USERNAME") or "").strip()
    pwd = (os.getenv("PMS_BASIC_PASSWORD") or "").strip()
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return f"Basic {token}"


def _count_nights(arrival: str | None, departure: str | None) -> int | None:
    try:
        return (date.fromisoformat(departure) - date.fromisoformat(arrival)).days  # type: ignore[arg-type]
    except Exception:
        return None


def _simplify_reservations(raw_data: list[dict]) -> list[dict]:
    out = []
    for res in raw_data:
        stays_out = []
        for stay in res.get("stays", []):
            days = stay.get("days", [])
            room = days[0].get("roomLabel", "") if days else ""
            treatment = (days[0].get("treatment") or {}).get("name", "") if days else ""

            guests_out = []
            for g in stay.get("guests", []):
                doc = g.get("identityDocument") or {}
                contacts = g.get("contacts") or {}
                guests_out.append({
                    "name": f"{g.get('name', '')} {g.get('surname', '')}".strip(),
                    "email": contacts.get("email"),
                    "phone": contacts.get("phone"),
                    "pre_checkin": bool(doc.get("number")),
                    "doc_type": (doc.get("type") or {}).get("name"),
                    "checked_in": (g.get("checkIn") or {}).get("status", False),
                    "checked_out": (g.get("checkOut") or {}).get("status", False),
                })

            addon_counts: dict[str, int] = {}
            for a in stay.get("addons", []):
                n = a.get("name", "Extra")
                addon_counts[n] = addon_counts.get(n, 0) + 1
            addons = [f"{n} x{c}" if c > 1 else n for n, c in addon_counts.items()]

            stay_notes = [
                note["note"]
                for note in stay.get("notes", [])
                if note.get("note")
            ]

            stays_out.append({
                "arrival": stay.get("arrivalDate"),
                "departure": stay.get("departureDate"),
                "room": room,
                "treatment": treatment,
                "checked_in": (stay.get("checkIn") or {}).get("status", False),
                "checked_out": (stay.get("checkOut") or {}).get("status", False),
                "guests": guests_out,
                "addons": addons,
                "notes": stay_notes,
            })

        res_notes = [
            note["note"]
            for note in res.get("notes", [])
            if note.get("note")
        ]

        booker = res.get("booker") or {}
        out.append({
            "id": res.get("id"),
            "status": res.get("status"),
            "source": (res.get("source") or {}).get("origin", {}).get("name"),
            "arrival": res.get("arrivalDate"),
            "departure": res.get("departureDate"),
            "nights": _count_nights(res.get("arrivalDate"), res.get("departureDate")),
            "booker": {
                "name": f"{booker.get('name', '')} {booker.get('surname', '')}".strip(),
                "email": booker.get("email"),
                "phone": booker.get("phone"),
            },
            "stays": stays_out,
            "notes": res_notes,
        })
    return out


def _pms_get_reservations_impl(
    arrival_date_from: str,
    arrival_date_to: str,
    departure_date_from: str,
    departure_date_to: str,
    stay_date: str,
    status: str,
) -> str:
    _tlog.info(
        "[TOOL] pms_get_reservations  arrival=%s→%s  departure=%s→%s  stay=%s  status=%s",
        arrival_date_from or "-", arrival_date_to or "-",
        departure_date_from or "-", departure_date_to or "-",
        stay_date or "-", status or "-",
    )
    _t0 = time.perf_counter()
    base_url = (os.getenv("PMS_BASEURL") or "").rstrip("/")
    property_id = (os.getenv("PMS_PROPERTY_ID") or os.getenv("PMS_PROPERTY") or "9091").strip()
    if not base_url:
        return "PMS non configurato (PMS_BASEURL mancante)."
    if not (os.getenv("PMS_BASIC_USERNAME") or "").strip():
        return "PMS non configurato (PMS_BASIC_USERNAME mancante)."

    timeout = _get_float_env("PMS_TIMEOUT_SECONDS", 30.0, 5.0, 120.0)
    headers = {
        "Authorization": _pms_auth_header(),
        "Accept": "application/json",
    }

    params: dict[str, str] = {
        "lang": "it",
        "propertyIds[]": property_id,
    }

    has_any_date = any([
        arrival_date_from, arrival_date_to,
        departure_date_from, departure_date_to,
        stay_date,
    ])
    if not has_any_date:
        params["arrivalDateFrom"] = date.today().isoformat()
    else:
        if arrival_date_from:
            params["arrivalDateFrom"] = arrival_date_from
        if arrival_date_to:
            params["arrivalDateTo"] = arrival_date_to
        if departure_date_from:
            params["departureDateFrom"] = departure_date_from
        if departure_date_to:
            params["departureDateTo"] = departure_date_to
        if stay_date:
            params["stayDateFrom"] = stay_date
            params["stayDateTo"] = stay_date

    if status:
        params["status[]"] = status

    all_reservations: list[dict] = []
    MAX_PAGES = 5
    for page in range(1, MAX_PAGES + 1):
        params["page"] = str(page)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.get(
                    f"{base_url}/api/v1/reservations",
                    headers=headers,
                    params=params,
                )
        except Exception as exc:
            return f"Errore di connessione al PMS: {exc}"

        if response.status_code == 401:
            return "Accesso al PMS non autorizzato (credenziali errate)."
        if response.status_code < 200 or response.status_code >= 300:
            return f"Errore PMS (HTTP {response.status_code})."

        try:
            body: Any = response.json()
        except Exception:
            return "Risposta non valida dal PMS."

        all_reservations.extend(body.get("data", []))

        meta = body.get("meta", {})
        if page >= meta.get("last_page", 1):
            break

    if not all_reservations:
        _tlog.info("[DONE] pms_get_reservations  %.1fs | 0 prenotazioni", time.perf_counter() - _t0)
        return "Nessuna prenotazione trovata per il periodo richiesto."

    simplified = _simplify_reservations(all_reservations)
    _tlog.info("[DONE] pms_get_reservations  %.1fs | %d prenotazioni", time.perf_counter() - _t0, len(simplified))
    return json.dumps(simplified, ensure_ascii=False, indent=2)


@tool(
    name="pms_get_reservations",
    description=(
        "Recupera le prenotazioni dell'hotel The Castelletto dal PMS Beddy. "
        "Usalo per domande su arrivi, partenze, ospiti in struttura, pre-checkin, "
        "camere assegnate, note speciali, animali, trattamenti."
    ),
    instructions=PMS_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def pms_get_reservations(
    arrival_date_from: str = "",
    arrival_date_to: str = "",
    departure_date_from: str = "",
    departure_date_to: str = "",
    stay_date: str = "",
    status: str = "Confirmed",
) -> str:
    return _pms_get_reservations_impl(
        arrival_date_from=arrival_date_from,
        arrival_date_to=arrival_date_to,
        departure_date_from=departure_date_from,
        departure_date_to=departure_date_to,
        stay_date=stay_date,
        status=status,
    )


# ---------------------------------------------------------------------------
# Reminders / scheduler
# ---------------------------------------------------------------------------

REMINDER_TOOL_PROMPT = (
    "Usa questi tool per creare, elencare ed eliminare pro-memoria schedulati.\n\n"
    "reminder_create — crea un reminder condizionale:\n"
    "  - name: nome univoco breve (es. 'check-colazione-7am')\n"
    "  - cron_expr: 5 campi cron, timezone Europe/Rome "
    "(es. '0 7 * * *' = ogni giorno alle 7, '15 7 * * 1-5' = lun-ven alle 7:15)\n"
    "  - task_description: istruzione completa per il run schedulato — deve descrivere "
    "COSA controllare (tool da usare, telecamera, ecc.) e COSA fare se la condizione è vera "
    "(es. inviare send_telegram_message al chat_id). Includi sempre il chat_id del destinatario.\n"
    "  - chat_id: ID Telegram numerico dell'utente da notificare\n\n"
    "reminder_list — elenca i reminder attivi\n"
    "reminder_delete — elimina un reminder per nome\n\n"
    "ESEMPI di task_description:\n"
    "  'Controlla la telecamera tc_kitchen con castelletto_camera_analyze e azione "
    "buffet_setup_detection. Se il buffet NON è rilevato e ci sono ospiti in struttura oggi "
    "(verifica con pms_get_reservations), invia send_telegram_message a chat_id 8594319243 "
    "con testo: Attenzione: sono le 7 e la colazione non sembra pronta.'\n"
)

_AGENT_ID = os.getenv("AGENT_ID", "marco-telegram-agent")


def _schedule_manager() -> ScheduleManager:
    from storage_data import get_sqlite_db
    return ScheduleManager(db=get_sqlite_db(_db_file()))


@tool(
    name="reminder_create",
    description=(
        "Crea un pro-memoria schedulato condizionale che gira automaticamente all'orario indicato. "
        "All'ora stabilita, l'agente esegue il task_description e notifica l'utente se la condizione è vera."
    ),
    instructions=REMINDER_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def reminder_create(name: str, cron_expr: str, task_description: str, chat_id: str) -> str:
    name = (name or "").strip()
    cron_expr = (cron_expr or "").strip()
    task_description = (task_description or "").strip()
    chat_id = (chat_id or "").strip()
    _tlog.info("[TOOL] reminder_create  name=%r  cron=%r  chat_id=%s", name, cron_expr, chat_id)
    _t0 = time.perf_counter()
    if not name or not cron_expr or not task_description or not chat_id:
        return "Errore: name, cron_expr, task_description e chat_id sono obbligatori."

    message = (
        f"{task_description}\n\n"
        f"[Nota: se devi inviare una notifica Telegram, il chat_id del destinatario è {chat_id}]"
    )
    payload = {
        "message": message,
        "session_id": "scheduler",
        "user_id": "system",
    }
    endpoint = f"/agents/{_AGENT_ID}/runs"
    try:
        schedule = _schedule_manager().create(
            name=name,
            cron=cron_expr,
            endpoint=endpoint,
            method="POST",
            description=task_description[:200],
            payload=payload,
            timezone="Europe/Rome",
            if_exists="update",
        )
        _tlog.info("[DONE] reminder_create  %.1fs | id=%s", time.perf_counter() - _t0, schedule.id)
        return (
            f"Reminder '{schedule.name}' creato.\n"
            f"Cron: {schedule.cron_expr} (Europe/Rome)\n"
            f"Descrizione: {schedule.description}"
        )
    except Exception as exc:
        _tlog.warning("[FAIL] reminder_create  %.1fs | %s", time.perf_counter() - _t0, exc)
        return f"Errore creazione reminder: {exc}"


@tool(
    name="reminder_list",
    description="Elenca tutti i pro-memoria schedulati attivi.",
    instructions=REMINDER_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def reminder_list() -> str:
    _tlog.info("[TOOL] reminder_list")
    _t0 = time.perf_counter()
    try:
        schedules = _schedule_manager().list()
    except Exception as exc:
        _tlog.warning("[FAIL] reminder_list  %.1fs | %s", time.perf_counter() - _t0, exc)
        return f"Errore lettura reminder: {exc}"

    if not schedules:
        _tlog.info("[DONE] reminder_list  %.1fs | 0 reminder", time.perf_counter() - _t0)
        return "Nessun reminder attivo."

    lines = [f"Reminder attivi ({len(schedules)}):"]
    for s in schedules:
        status = "✓" if s.enabled else "✗"
        next_run = ""
        if s.next_run_at:
            dt = datetime.fromtimestamp(s.next_run_at, tz=_ROME_TZ)
            next_run = f" | prossima esecuzione: {dt.strftime('%d/%m/%Y %H:%M')}"
        lines.append(f"  {status} {s.name} [{s.cron_expr}]{next_run}")
        if s.description:
            lines.append(f"     {s.description[:120]}")
    _tlog.info("[DONE] reminder_list  %.1fs | %d reminder", time.perf_counter() - _t0, len(schedules))
    return "\n".join(lines)


@tool(
    name="reminder_delete",
    description="Elimina un pro-memoria schedulato tramite il suo nome.",
    instructions=REMINDER_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def reminder_delete(name: str) -> str:
    name = (name or "").strip()
    _tlog.info("[TOOL] reminder_delete  name=%r", name)
    _t0 = time.perf_counter()
    if not name:
        return "Errore: specifica il nome del reminder da eliminare."
    try:
        schedules = _schedule_manager().list()
        target = next((s for s in schedules if s.name == name), None)
        if target is None:
            available = ", ".join(s.name for s in schedules) or "nessuno"
            return f"Reminder '{name}' non trovato. Reminder disponibili: {available}"
        _schedule_manager().delete(target.id)
        _tlog.info("[DONE] reminder_delete  %.1fs | deleted id=%s", time.perf_counter() - _t0, target.id)
        return f"Reminder '{name}' eliminato."
    except Exception as exc:
        _tlog.warning("[FAIL] reminder_delete  %.1fs | %s", time.perf_counter() - _t0, exc)
        return f"Errore eliminazione reminder: {exc}"


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

USER_MGMT_TOOL_PROMPT = (
    "Questi tool sono riservati agli amministratori. "
    "Vengono usati per aggiungere, elencare o eliminare gli utenti autorizzati al bot. "
    "Il controllo dei permessi avviene automaticamente: se l'utente non è admin riceverà un errore."
)


@tool(
    name="user_add",
    description="Aggiunge un utente all'anagrafica del bot tramite numero di cellulare (es. +393471234567). Solo per admin.",
    instructions=USER_MGMT_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def user_add(phone: str) -> str:
    _tlog.info("[TOOL] user_add  phone=%r", phone)
    _t0 = time.perf_counter()
    err = _check_admin()
    if err:
        return err

    phone = _normalize_phone(phone)
    if len(phone) < 8:
        return f"Numero non valido: '{phone}'."

    created, user = create_user_by_phone(_db_file(), phone)
    if not created:
        name = (user or {}).get("full_name") or ""
        label = f" ({name})" if name else ""
        _tlog.info("[DONE] user_add  %.1fs | already_exists phone=%s", time.perf_counter() - _t0, phone)
        return f"Utente {phone}{label} già presente in anagrafica."
    _tlog.info("[DONE] user_add  %.1fs | created phone=%s", time.perf_counter() - _t0, phone)
    return f"Utente {phone} aggiunto con ruolo 'user'. Potrà accedere al bot non appena condivide il suo contatto."


@tool(
    name="user_list",
    description="Restituisce l'elenco degli utenti registrati nel bot. Solo per admin.",
    instructions=USER_MGMT_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def user_list() -> str:
    _tlog.info("[TOOL] user_list")
    _t0 = time.perf_counter()
    err = _check_admin()
    if err:
        return err

    users = list_users(_db_file())
    if not users:
        _tlog.info("[DONE] user_list  %.1fs | 0 utenti", time.perf_counter() - _t0)
        return "Nessun utente registrato."

    lines = [f"Utenti registrati ({len(users)}):"]
    for u in users:
        name = u.get("full_name") or "—"
        phone = u.get("mobile_phone") or "—"
        role = u.get("role", "user")
        has_tg = "✓" if u.get("telegram_id") else "✗"
        lines.append(f"  • {name} | {phone} | {role} | Telegram: {has_tg}")
    _tlog.info("[DONE] user_list  %.1fs | %d utenti", time.perf_counter() - _t0, len(users))
    return "\n".join(lines)


@tool(
    name="user_delete",
    description="Elimina un utente dall'anagrafica tramite numero di cellulare. Non è possibile eliminare admin. Solo per admin.",
    instructions=USER_MGMT_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def user_delete(phone: str) -> str:
    _tlog.info("[TOOL] user_delete  phone=%r", phone)
    _t0 = time.perf_counter()
    err = _check_admin()
    if err:
        return err

    phone = _normalize_phone(phone)
    deleted = delete_user_by_phone(_db_file(), phone)
    if not deleted:
        _tlog.info("[DONE] user_delete  %.1fs | not_found phone=%s", time.perf_counter() - _t0, phone)
        return f"Utente {phone} non trovato o non eliminabile (gli admin non possono essere rimossi)."
    _tlog.info("[DONE] user_delete  %.1fs | deleted phone=%s", time.perf_counter() - _t0, phone)
    return f"Utente {phone} eliminato."


# ---------------------------------------------------------------------------
# Telegram: send notification message
# ---------------------------------------------------------------------------

TELEGRAM_SEND_TOOL_PROMPT = (
    "Usa questo tool per inviare un messaggio di notifica Telegram a un utente specifico.\n\n"
    "QUANDO USARLO:\n"
    "  - Esclusivamente durante l'esecuzione di un pro-memoria schedulato\n"
    "  - Solo quando la condizione verificata è soddisfatta (es. colazione non pronta, ospite in arrivo, ecc.)\n"
    "  - NON usarlo per le normali risposte alle conversazioni con l'utente\n\n"
    "PARAMETRI:\n"
    "  - chat_id: ID Telegram numerico del destinatario (es. '8594319243')\n"
    "  - message: testo del messaggio da inviare, chiaro e conciso in italiano"
)


@tool(
    name="send_telegram_message",
    description=(
        "Invia un messaggio di notifica Telegram a un utente. "
        "Da usare solo durante l'esecuzione di pro-memoria schedulati condizionali."
    ),
    instructions=TELEGRAM_SEND_TOOL_PROMPT,
    add_instructions=True,
    show_result=False,
)
def send_telegram_message(chat_id: str, message: str) -> str:
    _tlog.info("[TOOL] send_telegram_message  chat_id=%s  msg=%r", chat_id, message[:80])
    _t0 = time.perf_counter()
    token = (os.getenv("TELEGHRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return "Errore: token Telegram non configurato (TELEGRAM_BOT_TOKEN mancante)."

    chat_id = (chat_id or "").strip()
    if not chat_id:
        return "Errore: chat_id non specificato."

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = httpx.post(url, json={"chat_id": chat_id, "text": message}, timeout=15.0)
        data: Any = resp.json() if resp.content else {}
    except Exception as exc:
        _tlog.warning("[FAIL] send_telegram_message  %.1fs | %s", time.perf_counter() - _t0, exc)
        return f"Errore di connessione Telegram: {exc}"

    if resp.status_code == 200 and data.get("ok"):
        _tlog.info("[DONE] send_telegram_message  %.1fs | ok chat_id=%s", time.perf_counter() - _t0, chat_id)
        return f"Messaggio inviato a {chat_id}."
    _tlog.warning("[FAIL] send_telegram_message  %.1fs | HTTP %s %s", time.perf_counter() - _t0, resp.status_code, data.get("description", ""))
    return f"Errore Telegram (HTTP {resp.status_code}): {data.get('description', resp.text[:200])}"


def get_agent_tools() -> list[Any]:
    return [
        castelletto_camera_analyze,
        castelletto_list_actions,
        castelletto_list_sequences,
        castelletto_run_sequence,
        pms_get_reservations,
        reminder_create,
        reminder_list,
        reminder_delete,
        send_telegram_message,
        user_add,
        user_list,
        user_delete,
    ]
