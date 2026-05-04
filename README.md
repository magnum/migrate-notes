# Migrazione Note iCloud → Notion

Script Python standalone per migrare note `.md` esportate da iCloud (presenti nella tua cartella locale Google Drive) verso Notion, mantenendo la struttura cartelle e i link agli allegati.

## Cambio importante rispetto alla v1

Lo script ora **richiede** l'URL della pagina Notion che farà da "root" (quella che conterrà AI, Reference, Career, ecc.) — è obbligatorio passarlo via `--parent-url` o env var. Niente più ID hardcoded.

## Cosa fa

- Legge ricorsivamente la cartella `NOTES_ROOT`
- Per ogni cartella top-level (AI, Reference, ...) cerca una pagina già esistente con quel nome sotto il parent indicato; se non c'è la crea
- Per ogni `.md` crea una sotto-pagina Notion col contenuto pulito
- Sostituisce i riferimenti `images/foo.png` e `attachments/bar.pdf` con link Drive cliccabili
- **Idempotente**: se interrotto riparte da dove si era fermato (state in `.migration_state.json`)
- Salta automaticamente note vuote o con titolo "New Note"
- Salta cartelle `bookmark` e `Recently Deleted`

## Setup

### 1. Dipendenze

```bash
python3 -m venv venv
source venv/bin/activate
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

(Le librerie Google sono opzionali ma consigliate per i link agli allegati.)

### 2. Notion: crea integration e collega la pagina

1. https://www.notion.so/my-integrations → "New integration" → dagli un nome → copia il token (`secret_...`)
2. **Apri su Notion la pagina che farà da root** (quella che contiene AI, Reference, ecc.)
3. In alto a destra: **`•••` → "Connections" → "Add connections"** → seleziona la tua integration
4. Conferma. Tutte le sotto-pagine ereditano l'accesso.

### 3. (Opzionale) Drive API per gli allegati

Se vuoi che gli allegati nelle note diventino link Drive cliccabili:

1. https://console.cloud.google.com/ → crea progetto
2. APIs & Services → Library → cerca "Google Drive API" → Enable
3. Credentials → Create credentials → OAuth client ID
   - Application type: **Desktop app**
   - Scarica il JSON, rinominalo `credentials.json` e mettilo nella stessa cartella dello script
4. Al primo run lo script aprirà il browser per il consenso OAuth

Senza questo step, gli allegati appariranno come testo (non link cliccabili) — non blocca la migrazione.

### 4. Variabili d'ambiente

```bash
export NOTION_TOKEN="secret_xxx..."
export NOTES_ROOT="$HOME/Library/CloudStorage/GoogleDrive-antoniomolinari1977@gmail.com/My Drive/notes/iCloud"

# Opzionale: invece di passare --parent-url ogni volta
export NOTION_NOTES_PARENT_URL="https://www.notion.so/antoniomolinari/Notes-3560f748d2f680c9accbd9b6dadaf904"
```

## Uso

### Dry run (raccomandato la prima volta)

```bash
python3 migrate_notes_to_notion.py \
  --parent-url "https://www.notion.so/antoniomolinari/Notes-3560f748d2f680c9accbd9b6dadaf904?source=copy_link" \
  --dry-run
```

Non scrive su Notion, mostra solo cosa farebbe.

### Test su una sola cartella

```bash
python3 migrate_notes_to_notion.py \
  --parent-url "..." \
  --folder Reference \
  --dry-run

python3 migrate_notes_to_notion.py --parent-url "..." --folder Reference   # live
```

### Migrazione completa

```bash
python3 migrate_notes_to_notion.py --parent-url "..."
```

### Riprendere dopo un crash

Rilancia lo stesso comando — lo state file (`.migration_state.json`) tiene traccia di cosa è già stato migrato.

### Reset

```bash
python3 migrate_notes_to_notion.py --parent-url "..." --reset-state
```

> Nota: `--reset-state` cancella solo lo state file locale. Le pagine già create su Notion restano lì.

## Cosa controllare

Lo script genera nella sua cartella:

- `migration.log` — log dettagliato di ogni operazione
- `.migration_state.json` — stato persistente (cartelle Notion mappate, file già migrati, mappa allegati Drive). **Non cancellare** se vuoi mantenere idempotenza.

## Verifica iniziale dell'integration

All'avvio (in modalità live) lo script chiama `GET /pages/<parent_id>` per verificare l'accesso. Se fallisce con 404 (`object_not_found`), il messaggio te lo dice esplicitamente: l'integration non è collegata alla pagina. Vai sulla pagina root su Notion → `•••` → Connections → aggiungi.

## Personalizzazioni

All'inizio del file:

- `MIN_CONTENT_CHARS = 200` — soglia minima caratteri per non skippare una nota
- `SKIP_FOLDERS` — cartelle da ignorare
- `SKIP_TITLE_PATTERNS` — regex di titoli da ignorare

## Limiti noti

- Converter Markdown minimal: heading, liste, code fence, paragrafi. Bold/italic inline restano come testo plain con `**` letterali (Notion li renderà comunque, ma non sempre perfettamente).
- Tabelle Markdown vengono importate come paragrafi (Notion non supporta tabelle inline via API in modo banale).
- Rate limit Notion ~3 req/s: lo script fa pause da 0.35s tra una nota e l'altra.
- Note con >95 blocchi vengono splittate in più chiamate (Notion limita 100 children per request).
