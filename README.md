# Marco Agent — Guida operativa per il DevOps

## Cos'è

Marco Agent è un bot Telegram basato su LLM (Large Language Model) pensato per la gestione operativa dell'hotel **The Castelletto**. Risponde a messaggi di testo e immagini inviati dagli utenti registrati nel bot, utilizzando un set di tool specializzati.

Funzionalità principali:
- **Interrogazione telecamere** — analisi live e su finestre temporali tramite API Castelletto Vision
- **Controllo accessi armadietti** (piano -1) tramite sequenza dedicata sulla camera `tc_lockers`
- **Consultazione prenotazioni** tramite API PMS (Beddy)
- **Pianificazione reminder condizionali** — il bot può essere configurato per verificare condizioni a orari programmati e inviare notifiche proattive
- **Gestione utenti** — aggiunta, lista, rimozione utenti autorizzati (solo admin)
- **Memoria persistente per utente** — il contesto conversazionale viene mantenuto tra le sessioni in un database SQLite locale

Audio: se un utente invia un messaggio vocale, il bot risponde chiedendo di usare la dettatura del dispositivo.

---

## Architettura di runtime

```
Telegram API (polling)
       │
       ▼
telegram_bot.py (FastAPI + uvicorn, porta 7777)
       │
       ├── agent.py          — LLM agent (agno framework, OpenAI-compatible)
       ├── tools.py          — Tool: Castelletto API, PMS, scheduler, gestione utenti
       ├── storage_data.py   — SQLite: memoria, sessioni, utenti registrati
       └── utilities.py      — Preprocessing immagini, parser markdown Telegram
```

Il bot opera in modalità **polling** (getUpdates): non espone endpoint pubblici verso Telegram, si connette lui stesso all'API Telegram ogni `TELEGRAM_POLLING_TIMEOUT_SECONDS` secondi.

Il server HTTP locale (porta 7777) è usato internamente da agno per la comunicazione tra il bot e lo scheduler dei reminder. Non deve essere esposto su internet.

---

## Connessioni di rete in uscita

Il container apre connessioni verso:

| Destinazione | Protocollo | Scopo |
|---|---|---|
| `api.telegram.org` | HTTPS/443 | Polling aggiornamenti Telegram |
| `LLM_ENDPOINT` (configurabile) | HTTPS/443 | Inferenza LLM |
| `CASTELLETTO_API_BASE_URL` (configurabile) | HTTPS/443 | Telecamere e sequenze Castelletto |
| `PMS_BASEURL` (configurabile) | HTTPS/443 | Prenotazioni Beddy PMS |

Nessun servizio di terze parti riceve telemetria (telemetria agno disabilitata).

---

## Porte

| Porta | Direzione | Necessaria esternamente |
|---|---|---|
| **7777/tcp** | inbound (container) | **No** — solo comunicazione interna agno scheduler |

La porta 7777 non va pubblicata verso internet. Se il container è su Docker: **non usare** `-p 7777:7777` in produzione a meno di non avere un reverse proxy che la protegge con autenticazione.

Se si usa un reverse proxy che espone la 7777, tutte le chiamate alle route `/v1/agents/`, `/v1/sessions/`, `/v1/memory/` richiedono l'header:
```
Authorization: Bearer <OS_SECURITY_KEY>
```

---

## Persistenza dati

Il container scrive su due path che **devono essere montati come volume**:

| Path nel container | Contenuto |
|---|---|
| `/data/memory.sqllite` | Database SQLite: memoria utenti, sessioni, utenti registrati, reminder |
| `/data/temp/` | File temporanei (immagini ridimensionate pre-invio LLM, eliminate dopo l'uso) |

Senza volume i dati vengono persi a ogni restart del container.

---

## Avvio con Docker

```bash
# Crea il volume (una sola volta)
docker volume create marco-agent-data

# Avvio
docker run -d \
  --name marco-agent \
  --restart unless-stopped \
  --env-file .env \
  -v marco-agent-data:/data \
  marco-agent:latest
```

Log in tempo reale:
```bash
docker logs -f marco-agent
```

---

## Configurazione — file `.env`

Il file `.env` non è mai incluso nell'immagine Docker. Va fornito a runtime con `--env-file .env`.

### Chiavi obbligatorie

Senza queste il container si avvia ma crasha subito.

| Chiave | Descrizione |
|---|---|
| `LLM_ENDPOINT` | URL base dell'endpoint LLM (es. `https://api-gb10.elettra.ai`) |
| `LLM_MODEL` | Identificatore modello (es. `Galene/LLM`) |
| `LLM_APIKEY` | API key per l'endpoint LLM |
| `TELEGHRAM_BOT_TOKEN` | Token del bot Telegram (da BotFather) |
| `CASTELLETTO_API_BASE_URL` | URL base API Castelletto Vision |
| `CASTELLETTO_API_KEY` | API key Castelletto Vision |
| `PMS_BASEURL` | URL base API PMS Beddy |
| `PMS_BASIC_USERNAME` | Username autenticazione Basic PMS |
| `PMS_BASIC_PASSWORD` | Password autenticazione Basic PMS |
| `PMS_PROPERTY_ID` | ID property nel PMS |
| `OS_SECURITY_KEY` | Bearer token per proteggere le route HTTP interne di agno |

### Chiavi opzionali con default

| Chiave | Default | Descrizione |
|---|---|---|
| `AGNO_MEMORY_DB_FILE` | `memory.sqllite` | Path del database SQLite (nel container: `/data/memory.sqllite`) |
| `AGNO_DOCS_ENABLED` | `false` | Abilitare `/docs` e `/redoc` FastAPI (lasciare `false` in produzione) |
| `TELEGRAM_MODE` | `polling` | Modalità aggiornamenti: `polling` o `webhook` |
| `TELEGRAM_POLLING_TIMEOUT_SECONDS` | `15` | Timeout long-polling verso Telegram |
| `TELEGRAM_STREAMING` | `true` | Streaming delle risposte Telegram |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN` | — | Obbligatorio solo in modalità webhook: token segreto inviato da Telegram come header di verifica |
| `LLM_TIMEOUT_SECONDS` | `120` | Timeout chiamate LLM |
| `LLM_SEND_MEDIA_TO_MODEL` | `true` | Invia immagini direttamente al modello LLM |
| `LLM_STORE_MEDIA` | `false` | Archivia media sul server agno |
| `SCHEDULER_POLL_INTERVAL` | `60` | Intervallo (secondi) polling scheduler reminder |
| `IMAGE_MAX_DIM_PX` | `1000` | Dimensione massima lato lungo immagine prima dell'invio LLM |
| `IMAGE_JPEG_QUALITY` | `80` | Qualità JPEG compressione immagini |
| `IMAGE_DESCRIBE_MODEL` | `Galene/LLM` | Modello usato per descrivere immagini |
| `IMAGE_DESCRIBE_MAX_TOKENS` | `220` | Token massimi per la descrizione immagini |
| `IMAGE_ADD_HISTORY_TO_CONTEXT` | `false` | Includere cronologia conversazione nel contesto immagine |
| `CASTELLETTO_API_TIMEOUT_SECONDS` | `30` | Timeout chiamate API Castelletto |
| `PMS_TIMEOUT_SECONDS` | `30` | Timeout chiamate API PMS |
| `NUM_HISTORY_RUNS` | `6` | Numero di run precedenti inclusi nel contesto LLM |
| `MEMORY_CAPTURE_INSTRUCTIONS` | (built-in) | Istruzioni per il modulo memoria su cosa memorizzare |
| `MEMORY_ADDITIONAL_INSTRUCTIONS` | — | Istruzioni aggiuntive per il modulo memoria |
| `ENABLE_AGNO_SKILLS` | `true` | Abilitare agno skills da file YAML |
| `AGNO_SKILLS_DIR` | `.agents/skills` | Directory delle agno skills |
| `TRANSIT_TEMP_DIR` | `./temp` | Directory file temporanei (nel container: `/data/temp`) |
| `IMAGE_TEMP_DIR` | `./temp` | Directory temporanea immagini (nel container: `/data/temp`) |
| `ADMIN_TELEGRAM_CHAT_ID` | — | Chat ID Telegram dell'admin (per notifiche di sistema) |
| `APP_ENV` | `development` | Ambiente: `development` o `production` |
| `LOG_LEVEL` | `INFO` | Livello log: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PORT` | `7777` | Porta HTTP del server interno |
| `AGENT_ID` | `marco-telegram-agent` | Identificatore agente nel DB |
| `AGENT_NAME` | `Marco Telegram Bot` | Nome agente |

---

## Gestione utenti

Gli utenti del bot sono censiti nel database SQLite. L'accesso è a invito: al primo messaggio il bot chiede di condividere il numero di telefono tramite il pulsante Telegram nativo.

- Il **primo utente registrato** ha ruolo `admin`
- Solo gli admin possono aggiungere o rimuovere altri utenti tramite i comandi del bot
- Il database degli utenti si trova nel volume `/data/memory.sqllite` — un backup periodico di questo file è sufficiente per preservare l'intera configurazione operativa

---

## Health check

Il server espone `/health`:

```bash
curl http://localhost:7777/health
```

Risposta attesa: `{"status": "ok"}` (o equivalente agno).

Configurazione Docker health check consigliata:

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:7777/health || exit 1
```

---

## Build dell'immagine

```bash
# Build locale (ARM64 nativo su Apple Silicon / server ARM)
docker build -t marco-agent:latest .

# Build multi-arch per registry che distribuisce su ARM64 e AMD64
docker buildx build \
  --platform linux/arm64,linux/amd64 \
  -t marco-agent:latest \
  --push .
```
