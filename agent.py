import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.memory import MemoryManager
from agno.models.openai.like import OpenAILike
from agno.skills import LocalSkills, Skills
from storage_data import initialize_database, get_sqlite_db
from tools import get_agent_tools


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    joined = ", ".join(names)
    raise RuntimeError(f"Missing required environment variable: one of [{joined}]")


def _normalize_openai_base_url(endpoint: str) -> str:
    raw = endpoint.strip().rstrip("/")
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"Invalid LLM_ENDPOINT: {endpoint}")

    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        return raw
    return f"{raw}/v1"


def _default_memory_capture_instructions() -> str:
    return (
        "Salva solo memorie utili e stabili sull'utente.\n"
        "Includi: preferenze, obiettivi, interessi, vincoli, decisioni, contesto personale utile nel tempo.\n"
        "Non includere: dati sensibili inutili, password/token, dettagli temporanei o rumore conversazionale.\n"
        "Se un dato viene corretto dall'utente, aggiorna la memoria sostituendo quella vecchia.\n"
        "Per i riferimenti personali usa l'identita' utente collegata a user_id Telegram."
    )


def _memory_capture_instructions() -> str:
    value = os.getenv("MEMORY_CAPTURE_INSTRUCTIONS")
    if value and value.strip():
        return value.strip()
    return _default_memory_capture_instructions()


def _memory_additional_instructions() -> str | None:
    value = os.getenv("MEMORY_ADDITIONAL_INSTRUCTIONS")
    if value and value.strip():
        return value.strip()
    return None


def _agent_operational_instructions() -> list[str]:
    return [
        (
            "Per domande sulla situazione attuale dell'hotel The Castelletto (es. colazione, parcheggio, pulizie, presenze "
            "in cucina/lobby/patio/ingresso) usa il tool `castelletto_camera_analyze` scegliendo la telecamera "
            "e l'azione più appropriate. "
            "Per analisi su finestre temporali di video (es. 'qualcuno ha aperto l'armadio?', 'cosa è successo alle 10:30?') "
            "usa `castelletto_run_sequence` con camera_id, sequence_id e opzionalmente l'orario nel campo 'at'. "
            "Usa `castelletto_list_actions` o `castelletto_list_sequences` se non sei sicuro di quali azioni/sequenze esistono. "
            "Rispondi sempre in italiano con un'interpretazione naturale del risultato."
        ),
        (
            "Per domande sulle prenotazioni dell'hotel The Castelletto (arrivi, partenze, ospiti in struttura, "
            "pre-checkin, note, animali, camere) usa il tool `pms_get_reservations`. "
            "Se l'utente non specifica un periodo, chiedi 'per quale giorno o periodo?' prima di chiamare il tool. "
            "Sii prudente con i dati degli ospiti: non condividere email, telefono o dati personali "
            "a meno che l'utente non li chieda esplicitamente."
        ),
        (
            "Per creare pro-memoria condizionali usa reminder_create. "
            "Parametri: name (nome breve univoco), cron_expr (cron 5 campi, timezone Europe/Rome), "
            "task_description (istruzione completa per il controllo e l'eventuale notifica), "
            "chat_id (ID Telegram dell'utente da notificare). "
            "Nella task_description specifica: quale tool usare (es. castelletto_camera_analyze), "
            "quale azione/telecamera, la condizione da verificare, e di usare send_telegram_message "
            "con il chat_id se la condizione è soddisfatta. "
            "Esempi cron (Europe/Rome): '0 7 * * *' ogni giorno alle 7, '15 7 * * 1-5' lun-ven alle 7:15. "
            "Usa reminder_list per elencare i reminder attivi, reminder_delete per eliminarne uno. "
            "Durante l'esecuzione di un reminder, usa send_telegram_message SOLO se la condizione è vera."
        ),
        (
            "Per gestire gli utenti del bot usa i tool user_add, user_list, user_delete. "
            "Questi tool sono riservati agli amministratori: il controllo avviene automaticamente nel tool. "
            "user_add accetta un numero di cellulare completo (es. +393471234567): "
            "se l'utente esiste già lo segnala, altrimenti lo crea con ruolo 'user'. "
            "user_list mostra tutti gli utenti registrati. "
            "user_delete rimuove un utente per numero di cellulare (gli admin non possono essere eliminati). "
            "Usa questi tool quando l'utente chiede di aggiungere, elencare o rimuovere utenti dal bot."
        ),
        (
            "Rispondi sempre in modo conciso e operativo, evitando di essere prolisso o di ripetere informazioni già presenti nella memoria. "
            "Usa sempre la stessa lingua della richiesta dell'utente (italiano o inglese)."
        ),
    ]


@dataclass(frozen=True)
class AgentContext:
    agent: Agent
    agent_id: str
    llm_endpoint: str
    llm_model: str
    db_file: str
    db: SqliteDb
    memory_capture_instructions: str
    memory_additional_instructions: str | None
    skills_dir: str | None
    skill_names: list[str]


def _build_skills() -> tuple[Skills | None, str | None, list[str]]:
    if not _bool_env("ENABLE_AGNO_SKILLS", True):
        return None, None, []

    skills_dir = (os.getenv("AGNO_SKILLS_DIR") or ".agents/skills").strip() or ".agents/skills"
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        return None, str(skills_path), []

    try:
        skills = Skills(loaders=[LocalSkills(str(skills_path))])
    except Exception:
        return None, str(skills_path), []
    return skills, str(skills_path), list(skills.get_skill_names())


def build_agent_context() -> AgentContext:
    llm_endpoint = require_env("LLM_ENDPOINT")
    llm_model = require_env("LLM_MODEL")
    llm_api_key = require_env("LLM_APIKEY")

    db_file = os.getenv("AGNO_MEMORY_DB_FILE", "memory.sqllite")
    base_url = _normalize_openai_base_url(llm_endpoint)
    agent_id = os.getenv("AGENT_ID", "marco-telegram-agent")

    initialize_database(db_file)
    db = get_sqlite_db(db_file)

    memory_capture_instructions = _memory_capture_instructions()
    memory_additional_instructions = _memory_additional_instructions()
    skills, skills_dir, skill_names = _build_skills()

    _llm_timeout = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))

    memory_manager = MemoryManager(
        model=OpenAILike(
            id=llm_model,
            api_key=llm_api_key,
            base_url=base_url,
            timeout=_llm_timeout,
        ),
        memory_capture_instructions=memory_capture_instructions,
        additional_instructions=memory_additional_instructions,
        db=db,
    )

    agent = Agent(
        id=agent_id,
        name=os.getenv("AGENT_NAME", "Marco Telegram Bot"),
        model=OpenAILike(
            id=llm_model,
            api_key=llm_api_key,
            base_url=base_url,
            timeout=_llm_timeout,
        ),
        instructions=_agent_operational_instructions(),
        skills=skills,
        tools=get_agent_tools(),
        db=db,
        memory_manager=memory_manager,
        update_memory_on_run=True,
        add_memories_to_context=True,
        add_history_to_context=True,
        num_history_runs=int(os.getenv("NUM_HISTORY_RUNS", "6")),
        send_media_to_model=_bool_env("LLM_SEND_MEDIA_TO_MODEL", True),
        store_media=_bool_env("LLM_STORE_MEDIA", False),
        add_datetime_to_context=True,
        markdown=True,
    )

    return AgentContext(
        agent=agent,
        agent_id=agent_id,
        llm_endpoint=llm_endpoint,
        llm_model=llm_model,
        db_file=db_file,
        db=db,
        memory_capture_instructions=memory_capture_instructions,
        memory_additional_instructions=memory_additional_instructions,
        skills_dir=skills_dir,
        skill_names=skill_names,
    )
