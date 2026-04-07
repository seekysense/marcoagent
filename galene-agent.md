# Interazione con gli Agenti Galene via API

Questo documento illustra come usare le API REST della piattaforma Galene per:
1. Ottenere la lista degli agenti disponibili e leggerne le descrizioni
2. Avviare una conversazione con un agente specifico
3. Portare avanti la conversazione mantenendo il contesto dei turni precedenti

**Base URL:** `https://api-gb10.elettra.ai`

**Autenticazione:** tutte le chiamate richiedono un token JWT nell'header:
```
Authorization: Bearer <TOKEN>
```

---

## Fase 1 — Lista degli agenti e lettura delle descrizioni

### 1.1 Ottenere tutti gli agenti accessibili
```http
GET /agents
Authorization: Bearer <TOKEN>
```

La risposta contiene gli agenti di proprietà dell'utente, condivisi con lui e
quelli dell'organizzazione. Per ogni agente sono restituiti i campi essenziali:
`agent_id`, `name`, `description`, `ready`, `agent_type`, ecc.

**Risposta (200):**
```json
{
  "success": true,
  "message": "User agent list",
  "result": [
    {
      "agent_id": "agent-uuid-123",
      "agent_type": "personal",
      "owner_id": "user-uuid-456",
      "organization_id": "org-uuid-789",
      "name": "Research Assistant",
      "description": "AI assistant for academic research and literature review",
      "ready": true,
      "editable": true,
      "mcp": false,
      "database_connectors": true,
      "kb_connectors": false,
      "created_at": 1640995200,
      "updated_at": 1640995300
    }
  ]
}
```

> **Nota:** il campo `ready` indica che l'agente ha tutti gli allegati processati
> ed è pronto a rispondere. Filtrare su `ready: true` prima di procedere.

---

### 1.2 Leggere il dettaglio di un singolo agente

Per accedere alla descrizione estesa, al profilo (system prompt), agli allegati
e alle risorse connesse di un agente specifico:
```http
GET /agent/{agent_id}
Authorization: Bearer <TOKEN>
```

**Parametro path:**
| Parametro  | Tipo   | Obbligatorio | Descrizione               |
|------------|--------|:------------:|---------------------------|
| `agent_id` | string | ✅           | UUID univoco dell'agente  |

**Risposta (200):**
```json
{
  "success": true,
  "message": "Agent details retrieved",
  "result": {
    "agent_id": "agent-uuid-123",
    "name": "Research Assistant",
    "description": "AI assistant for academic research and literature review",
    "job_profile": "You are a research assistant specialized in...",
    "attachments": [],
    "mcp_servers": [],
    "database_connectors": [],
    "kb_connectors": [],
    "editable": true,
    "created_at": 1640995200,
    "updated_at": 1640995300
  }
}
```

I campi più rilevanti per capire le capacità dell'agente sono:
- `description` — descrizione leggibile dall'utente
- `job_profile` — system prompt che definisce il comportamento dell'agente
- `attachments` — file di conoscenza caricati
- `kb_connectors` / `database_connectors` / `mcp_servers` — risorse esterne connesse

---

## Fase 2 — Avviare la conversazione con un agente

### 2.1 Creare un nuovo thread di conversazione
```http
GET /conversation/init
Authorization: Bearer <TOKEN>
```

Crea un thread vuoto con UUID univoco. Non accetta parametri.

**Risposta (200):**
```json
{
  "success": true,
  "message": "Conversation created successfully",
  "result": {
    "conversation_id": "conv-uuid-123456"
  }
}
```

Conserva il `conversation_id`: servirà in ogni chiamata successiva.

---

### 2.2 Associare l'agente alla conversazione
```http
PATCH /conversation/{conversation_id}
Authorization: Bearer <TOKEN>
Content-Type: application/json
```

**Parametro path:**
| Parametro         | Tipo   | Obbligatorio | Descrizione                     |
|-------------------|--------|:------------:|---------------------------------|
| `conversation_id` | string | ✅           | ID ottenuto al passo precedente |

**Body (almeno `agent_id` è necessario, gli altri campi sono opzionali):**
```json
{
  "agent_id": "agent-uuid-123",
  "title": "Sessione di ricerca bibliografica",
  "personalization_language": "it"
}
```

Campi principali del body:
| Campo                      | Tipo    | Descrizione                                              |
|----------------------------|---------|----------------------------------------------------------|
| `agent_id`                 | string  | ID dell'agente da usare. `null` = agente di default      |
| `title`                    | string  | Titolo descrittivo della conversazione                   |
| `personalization_knowledge`| string  | Contesto aggiuntivo sull'utente                          |
| `personalization_behaviour`| string  | Istruzioni sul tono/stile delle risposte                 |
| `personalization_language` | string  | Lingua preferita per le risposte (codice ISO 639-1)      |
| `web_search`               | boolean | Abilita la ricerca web in tempo reale                    |
| `deep_reasoning`           | boolean | Abilita il ragionamento avanzato per problemi complessi  |
| `code_execution`           | boolean | Abilita l'esecuzione di codice                           |

**Risposta (200):**
```json
{
  "success": true,
  "message": "Conversation updated",
  "result": []
}
```

---

## Fase 3 — Conversazione multi-turno con l'agente

La conversazione avviene tramite l'endpoint compatibile OpenAI. Il collegamento
alla conversazione (e quindi all'agente associato) viene stabilito passando il
`conversation_id` come `session` nel campo `metadata`.

### 3.1 Primo turno — invio del messaggio iniziale
```http
POST /v1/chat/completions
Authorization: Bearer <TOKEN>
Content-Type: application/json
```

**Body:**
```json
{
  "model": "RedHatAI/Mistral-Small-3.1-24B",
  "messages": [
    {
      "role": "user",
      "content": "Quali sono le ultime ricerche sul cambiamento climatico in Antartide?"
    }
  ],
  "stream": false,
  "metadata": {
    "session": "conv-uuid-123456",
    "user_id": "user-uuid-456"
  }
}
```

Campi principali del body:
| Campo         | Tipo    | Obbligatorio | Descrizione                                                  |
|---------------|---------|:------------:|--------------------------------------------------------------|
| `model`       | string  | ✅           | Modello LLM da usare (vedi `GET /v1/models` per la lista)    |
| `messages`    | array   | ✅           | Lista dei messaggi: ogni elemento ha `role` e `content`      |
| `stream`      | boolean |              | `true` per ricevere la risposta in streaming (SSE)           |
| `metadata.session` | string | | `conversation_id` — collega la chiamata alla conversazione   |
| `metadata.user_id` | string | | Identificativo dell'utente chiamante                         |

Il campo `role` può essere `"user"`, `"assistant"` o `"system"`.

**Risposta (200):**
```json
{
  "id": "chatcmpl-456",
  "object": "chat.completion",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "Le ricerche più recenti sull'Antartide mostrano..."
      },
      "finish_reason": "stop",
      "index": 0
    }
  ],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 148,
    "total_tokens": 173
  }
}
```

---

### 3.2 Turni successivi — mantenere il contesto

Per ogni turno successivo, ricostruire l'array `messages` includendo **tutti i
messaggi scambiati in precedenza**, nell'ordine cronologico. Questo è il
meccanismo standard delle chat LLM per mantenere il contesto.
```http
POST /v1/chat/completions
Authorization: Bearer <TOKEN>
Content-Type: application/json
```

**Body (secondo turno, esempio):**
```json
{
  "model": "RedHatAI/Mistral-Small-3.1-24B",
  "messages": [
    {
      "role": "user",
      "content": "Quali sono le ultime ricerche sul cambiamento climatico in Antartide?"
    },
    {
      "role": "assistant",
      "content": "Le ricerche più recenti sull'Antartide mostrano..."
    },
    {
      "role": "user",
      "content": "Puoi approfondire l'impatto sul livello del mare?"
    }
  ],
  "stream": false,
  "metadata": {
    "session": "conv-uuid-123456",
    "user_id": "user-uuid-456"
  }
}
```

> **Importante:** il `conversation_id` nel campo `metadata.session` deve rimanere
> lo stesso per tutta la sessione. La piattaforma usa questo ID per recuperare il
> contesto della conversazione lato server e applicare le impostazioni dell'agente.

**Body (terzo turno e successivi — schema generico):**
```json
{
  "model": "RedHatAI/Mistral-Small-3.1-24B",
  "messages": [
    { "role": "user",      "content": "<messaggio turno 1>" },
    { "role": "assistant", "content": "<risposta turno 1>" },
    { "role": "user",      "content": "<messaggio turno 2>" },
    { "role": "assistant", "content": "<risposta turno 2>" },
    { "role": "user",      "content": "<nuovo messaggio>" }
  ],
  "stream": false,
  "metadata": {
    "session": "conv-uuid-123456",
    "user_id": "user-uuid-456"
  }
}
```

---

## Riepilogo del flusso completo
```
┌─────────────────────────────────────────────────────────────┐
│ 1. GET /agents                                              │
│    → lista agenti con name, description, agent_id          │
│                                                             │
│ 2. GET /agent/{agent_id}   [opzionale, per dettagli]        │
│    → description, job_profile, risorse connesse             │
│                                                             │
│ 3. GET /conversation/init                                   │
│    → conversation_id (nuovo thread vuoto)                   │
│                                                             │
│ 4. PATCH /conversation/{conversation_id}                    │
│    body: { "agent_id": "..." }                              │
│    → associa l'agente al thread                             │
│                                                             │
│ 5. POST /v1/chat/completions   ──── turno 1                 │
│    body: { messages: [U1], metadata: { session: conv_id } } │
│    → risposta A1                                            │
│                                                             │
│ 6. POST /v1/chat/completions   ──── turno 2                 │
│    body: { messages: [U1,A1,U2], metadata: { session: ... }}│
│    → risposta A2                                            │
│                                                             │
│ 7. ... (ripetere aggiungendo ogni coppia U/A ai messages)   │
└─────────────────────────────────────────────────────────────┘
```

---

## Codice di esempio (Python)
```python
import requests

BASE_URL = "https://api-gb10.elettra.ai"
TOKEN    = "<il-tuo-jwt-token>"
HEADERS  = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# ── 1. Lista agenti ──────────────────────────────────────────────────────────
resp = requests.get(f"{BASE_URL}/agents", headers=HEADERS)
agents = resp.json()["result"]

for a in agents:
    print(f"[{a['agent_id']}] {a['name']} — {a['description']}")

# ── 2. Dettaglio agente scelto ───────────────────────────────────────────────
agent_id = agents[0]["agent_id"]
resp = requests.get(f"{BASE_URL}/agent/{agent_id}", headers=HEADERS)
agent_detail = resp.json()["result"]
print("System prompt:", agent_detail.get("job_profile", "—"))

# ── 3. Nuova conversazione ───────────────────────────────────────────────────
resp = requests.get(f"{BASE_URL}/conversation/init", headers=HEADERS)
conversation_id = resp.json()["result"]["conversation_id"]
print("Conversation ID:", conversation_id)

# ── 4. Associa l'agente alla conversazione ───────────────────────────────────
requests.patch(
    f"{BASE_URL}/conversation/{conversation_id}",
    headers=HEADERS,
    json={"agent_id": agent_id, "personalization_language": "it"}
)

# ── 5-N. Loop di conversazione multi-turno ───────────────────────────────────
MODEL    = "RedHatAI/Mistral-Small-3.1-24B"
messages = []   # accumulatore storico dei messaggi

while True:
    user_input = input("Tu: ").strip()
    if user_input.lower() in ("exit", "quit"):
        break

    messages.append({"role": "user", "content": user_input})

    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "metadata": {
            "session": conversation_id,
            "user_id": "my-user-id"
        }
    }

    resp = requests.post(f"{BASE_URL}/v1/chat/completions", headers=HEADERS, json=payload)
    resp.raise_for_status()

    assistant_msg = resp.json()["choices"][0]["message"]["content"]
    messages.append({"role": "assistant", "content": assistant_msg})

    print(f"Agente: {assistant_msg}\n")
```

---

## Errori comuni

| Codice | Causa probabile                                              |
|--------|--------------------------------------------------------------|
| 401    | Token JWT mancante, scaduto o non valido                     |
| 403    | L'utente non ha accesso all'agente o alla conversazione      |
| 404    | `agent_id` o `conversation_id` inesistente                   |
| 400    | Body della richiesta malformato o campi obbligatori mancanti |
| 500    | Errore interno del server                                    |