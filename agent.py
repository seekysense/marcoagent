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
            "Per richieste di approvazione acquisto/sostituzione/servizi a pagamento usa il tool "
            "`create_sharepoint_approval_task`."
        ),
        (
            "Se la richiesta fa riferimento a una foto inviata dall'utente, usa prima `describe_image` "
            "e poi inserisci nel `context_summary` i dettagli salienti emersi dalla foto insieme a un "
            "suggerimento operativo su come proseguire il task. Se l'utente chiede solo di allegarla, non descrivere l'immagine o il file."
        ),
        (
            "Per recuperare lo stato delle richieste, task o approvazioni dell'utente usa il tool `get_task_status`."
        ),
        (
            "Se l'utente invia un file (documento o immagine) e chiede di allegarlo a un task, "
            "usa il tool `add_task_attachment` specificando l'ID del task. "
            "Se l'ID non è presente nel messaggio o nel contesto, chiedilo all'utente prima di procedere."
        ),
        (
            "Per domande sulla situazione attuale dell'hotel The Castelletto (es. colazione, parcheggio, pulizie, presenze "
            "in cucina/lobby/patio/ingresso) usa il tool `castelletto_camera_analyze` scegliendo la telecamera "
            "e l'azione più appropriate. Rispondi sempre in italiano con un'interpretazione naturale del risultato."
        ),
        (
            "Rispondi sempre in modo conciso e operativo, evitando di essere prolisso o di ripetere informazioni già presenti nella memoria. "
            "Usa sempre la stessa lingua della richiesta dell'utente (italiano o inglese) e non tradurre mai le parole chiave dei tool (es. `create_sharepoint_approval_task`, `describe_image`, `get_task_status`) in italiano."
        ),
    ]


@dataclass(frozen=True)
class AgentContext:
    agent: Agent
    agent_id: str
    llm_endpoint: str
    llm_model: str
    db_file: str
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

    memory_manager = MemoryManager(
        model=OpenAILike(
            id=llm_model,
            api_key=llm_api_key,
            base_url=base_url,
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
        memory_capture_instructions=memory_capture_instructions,
        memory_additional_instructions=memory_additional_instructions,
        skills_dir=skills_dir,
        skill_names=skill_names,
    )
