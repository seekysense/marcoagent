# Telegram Bot con Agno

La logica di configurazione e costruzione dell'agente e' in [agent.py](/Users/andrea/Projects/Infinite/MarcoAgent/agent.py), mentre runtime Telegram/polling/webhook e' in [telegram_bot.py](/Users/andrea/Projects/Infinite/MarcoAgent/telegram_bot.py).

## 1) Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Variabili `.env`

Il bot legge questi parametri:

- `LLM_ENDPOINT`
- `LLM_MODEL`
- `LLM_APIKEY`
- `TELEGRAM_TOKEN` (oppure `TELEGHRAM_BOT_TOKEN`, per compatibilita')

Opzionali:

- `AGENT_ID` (default: `marco-telegram-agent`)
- `AGNO_MEMORY_DB_FILE` (default: `memory.sqllite`)
- `PORT` (default: `7777`)
- `APP_ENV` (default automatico: `development`)
- `LOG_LEVEL` (`INFO` o `DEBUG`)
- `CHECK_TELEGRAM_WEBHOOK_ON_STARTUP` (`true`/`false`)
- `TELEGRAM_MODE` (`polling` default, oppure `webhook`)
- `TELEGRAM_POLLING_TIMEOUT_SECONDS` (default: `15`)
- `TELEGRAM_DELETE_WEBHOOK_ON_POLLING_START` (`true` default)
- `MEMORY_CAPTURE_INSTRUCTIONS` (prompt per decidere cosa salvare in memoria utente)
- `MEMORY_ADDITIONAL_INSTRUCTIONS` (regole extra opzionali per memory manager)
- `WHISPER_MODEL` (default: `base`)
- `WHISPER_LANGUAGE` (es. `it`; vuoto = auto detect)
- `WHISPER_USE_MEMORY_LANGUAGE` (default: `true`, preferisce lingua utente letta dalla memoria Agno)
- `AUDIO_ADD_HISTORY_TO_CONTEXT` (default: `false`, per audio usa la sessione ma senza history nel prompt)
- `TRANSIT_TEMP_DIR` (default: `./temp`, directory temporanea generale per file in transito)
- `AUDIO_TEMP_DIR` (default: usa `TRANSIT_TEMP_DIR`)
- `LLM_SEND_MEDIA_TO_MODEL` (default: `true`, necessario per modelli vision)
- `LLM_STORE_MEDIA` (default: `false`)
- `ENABLE_AGNO_SKILLS` (default: `true`)
- `AGNO_SKILLS_DIR` (default: `.agents/skills`)
- `IMAGE_MAX_DIM_PX` (default: `1000`)
- `IMAGE_JPEG_QUALITY` (default: `80`)
- `IMAGE_TEMP_DIR` (default: usa `TRANSIT_TEMP_DIR`)
- `IMAGE_ADD_HISTORY_TO_CONTEXT` (default: `false`)
- `IMAGE_DESCRIBE_MODEL` (default: `LLM_MODEL`)
- `IMAGE_DESCRIBE_MAX_TOKENS` (default: `220`)
- `IMAGE_DESCRIBE_USER_PROMPT` (prompt opzionale per personalizzare la descrizione immagini)
- `AMAZON_SEARCH_TIMEOUT_SECONDS` (default: `20`)
- `AMAZON_SEARCH_MAX_RESULTS` (default: `5`)
- `AMAZON_FINDER_LLM_MODEL` (default: `LLM_MODEL`)
- `AMAZON_FINDER_LLM_MAX_TOKENS` (default: `320`)
- `AMAZON_FINDER_LLM_TIMEOUT_SECONDS` (default: `25`)
- `AMAZON_AFFILIATE_ID` (id affiliate Amazon opzionale, aggiunto ai link prodotto)
- `SHAREPOINT_TASK_WEBHOOK_URL` (webhook n8n per creare task approvativi)
- `SHAREPOINT_TASK_REQUESTER_EMAIL` (mittente task, es. `marco.agent@infinitearea.com`)
- `SHAREPOINT_TASK_APPROVER_EMAIL` (approvatore task, es. `andrea.menozzi@infinitearea.com`)
- `SHAREPOINT_TASK_STATUS` (default operativo, es. `Pending Approval`)
- `SHAREPOINT_TASK_TIMEOUT_SECONDS` (default: `20`)

## 3) Avvio

```bash
python telegram_bot.py
```

Di default il bot parte in `polling` e chiama Telegram `getUpdates` con timeout 15 secondi.

Quando riceve un audio (`voice`/`audio`):

- scarica il file Telegram
- lo salva in `AUDIO_TEMP_DIR` (default da `TRANSIT_TEMP_DIR`, quindi `./temp`)
- trascrive con Whisper locale (prima prova a usare la lingua preferita utente trovata in memoria)
- invia in chat la trascrizione citata
- chiama direttamente l'agente con il testo trascritto e invia la risposta
- per default non include la history nel prompt audio (`AUDIO_ADD_HISTORY_TO_CONTEXT=false`) per evitare regressioni dovute a media storici

Quando riceve una foto (`photo`):

- scarica il file Telegram
- ridimensiona lato massimo a `IMAGE_MAX_DIM_PX` (default `1000`)
- ricodifica in JPEG con qualità `IMAGE_JPEG_QUALITY` (default `80`)
- invia immagine ottimizzata + testo/caption all'agente
- invia la risposta dell'agente in chat

Il bot espone la webhook su:

- `POST /telegram/webhook`
- `GET /telegram/status`

## 4) Modalita' Webhook (opzionale)

Se vuoi usare webhook invece del polling:

```bash
export TELEGRAM_MODE=webhook
python telegram_bot.py
```

## 5) Esporre in HTTPS (locale) per webhook

Telegram richiede una URL pubblica HTTPS. In sviluppo puoi usare ngrok:

```bash
ngrok http 7777
```

Poi registra la webhook:

```bash
export NGROK_URL="https://<tuo-subdomain>.ngrok-free.app"
curl "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook?url=${NGROK_URL}/telegram/webhook"
```

## 6) Memoria persistente

La memoria Agno e le sessioni vengono salvate su SQLite locale nel file `memory.sqllite`.

## 7) Concetti Utente e Sessione

- `user_id`: viene preso automaticamente da `message.from.id` di Telegram.
- `session_id`: viene gestito dalla Telegram interface di Agno con scope:
  - `tg:<agent_id>:<chat_id>`
  - `tg:<agent_id>:<chat_id>:<thread_id>` (topic/thread)
- in DB, per lo stesso `user_id`, le memorie restano condivise tra sessioni diverse.

## 8) Prompt Memoria (cosa salvare)

Il bot usa un `MemoryManager` con `memory_capture_instructions`.
Se non imposti nulla, usa un prompt default che salva preferenze/obiettivi/vincoli e scarta rumore o dati sensibili.

Esempio `.env`:

```bash
MEMORY_CAPTURE_INSTRUCTIONS=Salva solo preferenze stabili, obiettivi, vincoli, interessi e correzioni esplicite dell'utente. Ignora dati sensibili e dettagli temporanei.
MEMORY_ADDITIONAL_INSTRUCTIONS=Non salvare mai token, password o dati finanziari completi.
```

## 9) Log utili

All'avvio vedrai:

- configurazione caricata (endpoint, modello, db)
- stato webhook Telegram corrente (`getWebhookInfo`)

Durante il runtime vedrai:

- richieste in ingresso su `/telegram/*`
- codice di risposta e tempo in ms
- per il polling: `update_id`, `user_id` Telegram e `session_scope` calcolato
- per audio: path locale file e stato trascrizione Whisper

## 10) Prerequisiti Whisper locale

Installa dipendenze Python:

```bash
pip install -r requirements.txt
```

Assicurati di avere `ffmpeg` installato nel sistema (richiesto da Whisper).

## 11) Tool Agno: SharePoint approvazioni

Il file [tools.py](/Users/andrea/Projects/Infinite/MarcoAgent/tools.py) definisce il tool:

- `describe_image`
- `find_amazon_product`
- `create_sharepoint_approval_task`

Il tool viene registrato in [agent.py](/Users/andrea/Projects/Infinite/MarcoAgent/agent.py) e chiama il webhook n8n con il payload richiesto:

- `Titolo`
- `Description`
- `Approver` (da `.env`)
- `TaskStatus` (da `.env`)
- `RequesterEmail` (da `.env`)
- `ContextSummary`

Flusso atteso su richieste con foto:

- l'agente usa prima `describe_image` per estrarre elementi salienti (oggetto, loghi/marchi, sigle, stato)
- poi, se serve trovare alternative/prodotti, usa `find_amazon_product`
- infine usa `create_sharepoint_approval_task` includendo nel `ContextSummary` la descrizione foto e un suggerimento operativo

Flusso `find_amazon_product`:

- effettua richiesta HTTP diretta su `amazon.it`
- parse HTML con BeautifulSoup (`div[data-component-type="s-search-result"]`)
- estrae ASIN, titolo, prezzo, valutazione, link
- passa i risultati estratti al LLM per una sintesi finale utile all'utente

Nel codice trovi un commento dedicato al prompt di attivazione del tool (`SHAREPOINT_APPROVAL_TOOL_PROMPT`) da personalizzare.

Output tool verso utente (formato minimale):

- successo: `Esito: creato` + `Titolo: ...`
- errore: `Esito: errore`

Nel `context_summary` il modello include anche un suggerimento operativo sintetico su come portare avanti il task.

## 12) Nota Amazon

Amazon puo' bloccare richieste automatiche (CAPTCHA / HTTP 503). In quel caso il tool restituisce errore operativo e va ritentata la ricerca.
