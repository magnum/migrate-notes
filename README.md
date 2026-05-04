# Notion ↔ Markdown Migration Tools

Two standalone Python scripts to move notes between **Notion** and a local folder of **Markdown files**, in both directions.

Originally built to migrate a personal iCloud Notes archive (exported as `.md` via [iCloud-Notes-Liberated](https://github.com/threeplanetssoftware/apple_cloud_notes_parser) or similar) stored in Google Drive into Notion, while preserving folder structure, attachments, and inline formatting. Designed to be **idempotent**, **resumable**, and **safe to re-run**.

## What's in this folder

- **`migrate_notes_to_notion.py`** — Markdown → Notion. Reads `.md` files from a local folder tree and creates a mirrored page hierarchy on Notion.
- **`migrate_notes_from_notion.py`** — Notion → Markdown. Walks a Notion page tree and writes a mirrored folder tree of `.md` files locally.

Both scripts use only the Python standard library for Notion API calls. Google Drive API libraries are optional (only the `to_notion` direction uses them, to map attachment filenames to Drive URLs).

## Common setup

### 1. Notion integration

1. Go to <https://www.notion.so/my-integrations>.
2. Click **"New integration"**, give it a name (e.g. `notes-migrator`), associate it with your workspace, and copy the secret token (`secret_...`).
3. Open the **root Notion page** you want to use (the page that will hold all top-level folders, or the root of the export):
   - Click the `•••` menu (top right) → **"Connections"** → **"Add connections"** → select your integration.
4. Sub-pages inherit access from their parent — you only connect the integration to the root page.

### 2. Environment variables

```bash
export NOTION_TOKEN="secret_xxxxxx..."

# Optional: avoids passing --parent-url every time
export NOTION_NOTES_PARENT_URL="https://www.notion.so/your-workspace/Notes-3560f748d2f680c9accbd9b6dadaf904"
```

### 3. (Optional) Google Drive API — only for `to_notion` direction

This is needed only if your Markdown notes reference attachments via `images/foo.png` or `attachments/bar.pdf` paths and you want them to become **clickable Drive links** in Notion.

```bash
pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

Then create OAuth credentials:

1. <https://console.cloud.google.com/> → create a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID** (type: **Desktop app**).
4. Download the JSON, rename it to `credentials.json`, and place it in the same folder as the scripts.
5. The first run opens a browser for OAuth consent, then caches credentials in `token.json`.

If you skip this step, attachments still appear in the migrated notes but as plain-text labels rather than links.

---

## `migrate_notes_to_notion.py` — Markdown → Notion

Walks a local folder of `.md` files and creates a mirrored page tree on Notion.

### What it does

- Scans `NOTES_ROOT` recursively, finds every `.md` file
- For each top-level subfolder (e.g. `AI/`, `Reference/`, `Career/`) creates or reuses a Notion page with the same name under `--parent-url`
- For each `.md` file creates a child Notion page with the cleaned-up content
- Strips iCloud's `\#`, `\*`, `\[`, etc. escape sequences
- Replaces `images/foo.png` and `attachments/bar.pdf` references with Drive view URLs (when Drive API is configured)
- Converts inline Markdown (links, bold, italic, code) to proper Notion `rich_text` segments
- Converts pipe tables to native Notion table blocks
- Recognises `- [ ]` / `- [x]` as Notion to-do blocks
- Skips empty notes (configurable threshold) and files named `New Note.md`
- Skips folders named `bookmark`, `Recently Deleted`, `attachments`, `images`
- Tracks progress in `.migration_state.json` so re-runs skip already-migrated notes
- Reuses already-existing Notion pages with matching titles (case-insensitive)

### Parameters

| Flag | Description |
|---|---|
| `--parent-url URL` | URL or ID of the Notion page that holds all top-level folders. Required (or set `NOTION_NOTES_PARENT_URL`). |
| `--dry-run` | Preview without writing anything to Notion. |
| `--folder NAME` | Only process this top-level folder under `NOTES_ROOT` (e.g. `--folder Reference`). |
| `--file PATH` | Only migrate this single `.md` file. Accepts an absolute path, a path relative to `NOTES_ROOT`, or a bare filename if unique. Mutually exclusive with `--folder`. |
| `--force` | With `--file`: re-migrate even if already on Notion or in state (creates a duplicate page). Useful for iterating on the converter against a single test note. |
| `--no-drive-api` | Skip Drive API attachment lookup (attachments become plain text). |
| `--reset-state` | Delete the local state file and start fresh. Does NOT delete pages already created on Notion. |

### Environment variables

| Variable | Default |
|---|---|
| `NOTION_TOKEN` | (required) |
| `NOTION_NOTES_PARENT_URL` | (used if `--parent-url` is omitted) |
| `NOTES_ROOT` | `~/Library/CloudStorage/GoogleDrive-.../My Drive/notes/iCloud` |
| `DRIVE_ROOT_FOLDER_ID` | Google Drive folder ID containing your notes (used for attachment indexing) |

### Examples

```bash
# Dry run — preview the whole migration
python3 migrate_notes_to_notion.py \
  --parent-url "https://www.notion.so/your-workspace/Notes-3560f748d2f680c9accbd9b6dadaf904" \
  --dry-run

# Migrate everything
python3 migrate_notes_to_notion.py \
  --parent-url "https://www.notion.so/your-workspace/Notes-3560f748d2f680c9accbd9b6dadaf904"

# Test on a single folder first
python3 migrate_notes_to_notion.py --parent-url "..." --folder Reference

# Test the converter on one note (and force re-creation if needed)
python3 migrate_notes_to_notion.py \
  --parent-url "..." \
  --file "Notes/2024-06-15-AI tools.md" \
  --force

# Skip the Drive API (faster, no attachment links)
python3 migrate_notes_to_notion.py --parent-url "..." --no-drive-api

# Resume after a crash — just re-run the same command, idempotent
python3 migrate_notes_to_notion.py --parent-url "..."
```

### State and logs

- `.migration_state.json` — tracks parent UUID, created folder pages, migrated files, cached Drive attachment map. **Don't delete** unless you want to start over.
- `migration.log` — per-operation log.

---

## `migrate_notes_from_notion.py` — Notion → Markdown

Walks a Notion page tree and writes a mirrored folder tree of `.md` files locally.

### What it does

- Starting from `--parent-url`, recursively walks all child pages
- For each page, generates a `.md` file with YAML frontmatter (title, source URL, Notion ID, timestamps)
- Layout rules:
  - **Leaf page** (no child pages) → `<export>/<title>.md`
  - **Page with only sub-pages, no body** → `<export>/<title>/` (folder, no file)
  - **Page with both body and sub-pages** → `<export>/<title>/index.md` plus sub-pages alongside
- Block conversion supports:
  - Headings, paragraphs, bulleted/numbered lists (with nesting), to-do, quote, callout, divider, code blocks (with language), tables, image/file/video/audio/pdf, bookmarks, embeds, equations
  - Inline formatting: links, bold, italic, strikethrough, inline code
- File and folder names are sanitized for cross-platform safety (`/`, `:`, `\`, etc. replaced)
- Tracks progress in `.export_state.json` so re-runs skip already-exported pages

### Parameters

| Flag | Description |
|---|---|
| `--parent-url URL` | URL or ID of the Notion page to export. Required (or set `NOTION_NOTES_PARENT_URL`). |
| `--export DIR` | Output directory. Default: `./export/`. |
| `--max-depth N` | Maximum recursion depth (default: unlimited). Useful for testing with `--max-depth 1` or `2`. |
| `--include-root` | Also export the parent page itself as `<export>/<parent-title>.md`. By default only its children are exported. |
| `--dry-run` | Walk the tree without writing files. |
| `--reset-state` | Delete state file and start fresh. Does NOT delete already-exported `.md` files on disk. |

### Environment variables

| Variable | Default |
|---|---|
| `NOTION_TOKEN` | (required) |
| `NOTION_NOTES_PARENT_URL` | (used if `--parent-url` is omitted) |

### Output format

Each `.md` file starts with YAML frontmatter:

```markdown
---
title: My Note Title
source: https://www.notion.so/...
notion_id: 3560f748-d2f6-80c9-accb-d9b6dadaf904
created: 2024-01-01T10:00:00.000Z
last_edited: 2024-06-15T14:30:00.000Z
---

# My Note Title

Note content here, with [links](https://example.com), **bold**, *italic*, and `code`...
```

### Examples

```bash
# Default — export to ./export/
python3 migrate_notes_from_notion.py \
  --parent-url "https://www.notion.so/your-workspace/Notes-3560f748d2f680c9accbd9b6dadaf904"

# Custom output directory
python3 migrate_notes_from_notion.py --parent-url "..." --export ./output/

# Dry run to see what would be exported
python3 migrate_notes_from_notion.py --parent-url "..." --dry-run

# Limit depth (useful for testing on a deep tree)
python3 migrate_notes_from_notion.py --parent-url "..." --max-depth 2

# Include the parent page itself in the export
python3 migrate_notes_from_notion.py --parent-url "..." --include-root

# Reset and re-export everything
python3 migrate_notes_from_notion.py --parent-url "..." --reset-state
```

### State and logs

- `.export_state.json` — tracks parent UUID and page-id → file path mapping.
- `export.log` — per-operation log.

---

## Safety & operational notes

- **Idempotent by design.** Both scripts are safe to interrupt and re-run. State is persisted after every successful operation.
- **`--reset-state` only clears local state**, never deletes content on Notion or already-exported files on disk.
- **Notion rate limits** (~3 req/s). The scripts pace themselves with ~0.35s between requests. Expect **~3-5 minutes for ~500 pages**.
- **Permissions race condition.** When the `to_notion` script creates a brand-new top-level folder page, Notion can briefly return 404 when listing its children due to permission propagation lag. The script handles this by skipping the children fetch for just-created pages and adding a small delay.
- **Notion content limits:** rich_text segments capped at 2000 chars (auto-truncated); pages split into multiple `PATCH /blocks` calls when exceeding 95 children per request.
- **Image URLs from Notion file uploads** (not external URLs) are temporary and expire after about an hour. The export script writes them as-is; if you need a self-contained export with downloaded media, that's a separate enhancement.

## Limitations

- **Inline formatting** is best-effort: the small Markdown subset covers links, bold, italic, code, autolinks, bare URLs, but does not handle every CommonMark edge case (nested emphasis, hard line breaks, footnotes, etc.).
- **Notion blocks not handled** in the export direction: synced blocks (children only), columns/column lists (rendered flat), template buttons.
- **Database content** (Notion databases as opposed to pages) is not exported as content — only a placeholder reference is written.
- **Round-tripping** (export → re-import) works for most blocks but is not byte-perfect; some Notion features (callout colours, complex nested toggles) lose fidelity.

## Quick troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| `404: Could not find block with ID...` | Integration not connected to the root page | Open the root page on Notion → `•••` → Connections → add your integration |
| `404` immediately after creating a folder | Notion permission propagation lag | Already handled; if it persists, increase the `time.sleep(1.0)` in `ensure_folder_page` |
| `NOTION_TOKEN env var not set` | Token missing | `export NOTION_TOKEN="secret_..."` |
| Links rendered as literal `[url](url)` text | Old run before the inline-parser fix | Update the script and re-run with `--force` on affected files |
| Tables rendered as plain pipe text | Old run before the table-parser fix | Update the script and re-run with `--force` on affected files |
| `Filename '...' is ambiguous, found N matches` | `--file` got just a filename that exists in multiple folders | Use a path: `--file "Reference/2024-01-01-foo.md"` |

## Files

```
.
├── migrate_notes_to_notion.py     # Markdown → Notion
├── migrate_notes_from_notion.py   # Notion → Markdown
├── README.md                      # this file
├── credentials.json               # (you provide; for Drive API attachment lookup)
├── token.json                     # (auto-generated after first OAuth consent)
├── .migration_state.json          # (auto-generated by migrate_notes_to_notion.py)
├── .export_state.json             # (auto-generated by migrate_notes_from_notion.py)
├── migration.log                  # (auto-generated)
└── export.log                     # (auto-generated)
```
