# Migrazione Note iCloud → Notion

Script Python standalone per migrare note `.md` esportate da iCloud (presenti nella tua cartella locale Google Drive) verso Notion, mantenendo struttura cartelle e link agli allegati.

## Cosa fa

- Legge ricorsivamente `~/Library/CloudStorage/GoogleDrive-.../My Drive/notes/iCloud`
- Per ogni cartella top-level (AI, Reference, Career, ...) crea/usa la pagina corrispondente su Notion sotto `notes`
- Per ogni `.md` crea una sotto-pagina Notion col contenuto pulito
- Sostituisce i riferimenti `images/foo.png` e `attachments/bar.pdf` con link Drive cliccabili
- **Idempotente**: se interrotto, riparte da dove si era fermato (state file `.migration_state.json`)
- Salta automaticamente note vuote o con titolo "New Note"
- Salta cartelle `bookmark` e `Recently Deleted`

## Setup

### 1. Installa dipendenze

```bash
cd ~/Downloads  # o dove preferisci
# crea virtualenv (opzionale ma consigliato)
python3 -m venv venv
source venv/bin/activate

# librerie Drive API (opzionali ma consigliate per i link agli allegati)
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

### 2. Crea un'integration Notion

1. Vai su https://www.notion.so/my-integrations
2. "New integration" → dai un nome (es. "Notes Migrator"), associa al tuo workspace
3. Copia il token (`secret_...`)
4. **Importante**: vai sulla pagina `notes` su Notion → menu `...` in alto a destra → "Add connections" → seleziona la tua integration. Senza questo l'API non può scrivere.

### 3. (Opzionale) Setup Google Drive API per gli allegati

Se vuoi che gli allegati delle note diventino link Drive cliccabili (consigliato):

1. https://console.cloud.google.com/ → crea progetto
2. APIs & Services → Library → cerca "Google Drive API" → Enable
3. APIs & Services → Credentials → Create credentials → OAuth client ID
   - Application type: **Desktop app**
   - Scaricare il JSON, rinominarlo `credentials.json` e metterlo nella stessa cartella dello script
4. Al primo run lo script aprirà il browser per il consenso OAuth, poi salverà `token.json` per i run successivi

Se salti questo step, gli allegati appariranno come testo (non link cliccabili).

### 4. Configura le variabili

```bash
export NOTION_TOKEN="secret_xxx..."
export NOTES_ROOT="$HOME/Library/CloudStorage/GoogleDrive-antoniomolinari1977@gmail.com/My Drive/notes/iCloud"
# Il parent ID della pagina "notes" su Notion è già nello script come default,
# ma puoi sovrascriverlo:
# export NOTION_NOTES_PARENT_ID="3550f748-d2f6-818f-aaf0-c4e40a2daf69"
```

## Uso

### Dry run (raccomandato la prima volta)

```bash
python3 migrate_notes_to_notion.py --dry-run
```

Questo NON scrive su Notion. Mostra solo cosa farebbe.

### Test su una sola cartella

```bash
python3 migrate_notes_to_notion.py --folder Reference --dry-run
python3 migrate_notes_to_notion.py --folder Reference  # live
```

### Migrazione completa

```bash
python3 migrate_notes_to_notion.py
```

Va avanti finché non finisce (o crashes; in quel caso rilancialo, riparte dal punto giusto).

### Saltare la Drive API (allegati come testo plain)

```bash
python3 migrate_notes_to_notion.py --no-drive-api
```

### Reset completo

```bash
python3 migrate_notes_to_notion.py --reset-state
```

## Cosa controllare

Lo script genera due file nella sua cartella:

- `migration.log` — log dettagliato di ogni operazione
- `.migration_state.json` — stato persistente (cartelle Notion create, file già migrati, mappa allegati Drive). **Non cancellare** se vuoi mantenere idempotenza.

## Personalizzazioni rapide

Tutte all'inizio del file `migrate_notes_to_notion.py`:

- `MIN_CONTENT_CHARS = 200` — soglia minima caratteri per non skippare una nota
- `SKIP_FOLDERS = {"bookmark", "Recently Deleted"}` — cartelle da ignorare
- `SKIP_TITLE_PATTERNS` — regex di titoli da ignorare (oltre a "New Note")
- `KNOWN_NOTION_FOLDERS` — mappa pre-popolata delle pagine Notion già esistenti, per non ricrearle

## Limiti noti

- Il converter Markdown è minimal: heading, bullet, numerati, code fence, paragrafi. Niente bold/italic inline (vengono lasciati come testo plain con `**` letterali — Notion li renderà comunque parzialmente).
- Note con tabelle Markdown vengono importate come testo (Notion non supporta tabelle inline via API in modo semplice).
- Rate limit Notion: ~3 req/s. Lo script fa pause da 0.35s tra una nota e l'altra. Per 200 note prevedi ~2-3 minuti.
- Se una nota supera i ~95 blocchi, vengono splittati in batch (Notion limita 100 children per request).

## Se qualcosa va storto

1. Guarda `migration.log` — gli errori specifici di una nota non bloccano il resto
2. Per ri-migrare una singola nota: cancella la sua entry da `.migration_state.json` (chiave = path assoluto del .md) e rilancia
3. Per ricominciare tutto: `--reset-state` (NON cancella le pagine già create su Notion, dovrai cancellarle a mano)
