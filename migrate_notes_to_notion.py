#!/usr/bin/env python3
"""
Migrate iCloud notes (exported as .md) from local Google Drive folder to Notion.

PREREQUISITES:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

USAGE:
    1. Set env vars (or pass via CLI):
       export NOTION_TOKEN="secret_..."
       export NOTES_ROOT="$HOME/Library/CloudStorage/GoogleDrive-.../My Drive/notes/iCloud"

    2. Get the URL of the Notion page that will hold all top-level folders
       (right-click the page in Notion -> Copy link).

    3. Run:
       python3 migrate_notes_to_notion.py --parent-url "https://www.notion.so/..."
       python3 migrate_notes_to_notion.py --parent-url "..." --dry-run
       python3 migrate_notes_to_notion.py --parent-url "..." --folder Reference

    Alternative: set NOTION_NOTES_PARENT_URL env var instead of passing --parent-url.

REQUIRED NOTION PERMISSIONS:
    The integration whose token is in NOTION_TOKEN must have access to the
    parent page. Open that page in Notion -> ... menu -> "Connections" ->
    add your integration. Sub-pages inherit access automatically.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DEFAULT_PARENT_URL = os.environ.get("NOTION_NOTES_PARENT_URL", "")

NOTES_ROOT = Path(
    os.environ.get(
        "NOTES_ROOT",
        os.path.expanduser(
            "~/Library/CloudStorage/GoogleDrive-antoniomolinari1977@gmail.com/My Drive/notes/iCloud"
        ),
    )
)

SKIP_FOLDERS = {"bookmark", "Recently Deleted", "attachments", "images"}
MIN_CONTENT_CHARS = 200
SKIP_TITLE_PATTERNS = [re.compile(r"^New Note(-\d+)?$")]

STATE_FILE = Path(__file__).parent / ".migration_state.json"
LOG_FILE = Path(__file__).parent / "migration.log"

DRIVE_ROOT_FOLDER_ID = os.environ.get(
    "DRIVE_ROOT_FOLDER_ID",
    "16Af6BH9-VyaJRPC9PzEVsuiOya0Zf5HW",
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str, level: str = "INFO") -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {level:5s} {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# URL / ID parsing
# ---------------------------------------------------------------------------

UUID_RE = re.compile(
    r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)


def extract_notion_id(url_or_id: str) -> str:
    """Extract a Notion page UUID from a URL or raw ID string.

    Accepts:
      - https://www.notion.so/Notes-3560f748d2f680c9accbd9b6dadaf904
      - https://www.notion.so/workspace/Notes-...?source=copy_link
      - 3560f748-d2f6-80c9-accb-d9b6dadaf904
      - 3560f748d2f680c9accbd9b6dadaf904
    Returns: dashed UUID.
    """
    s = url_or_id.strip()
    if not s:
        raise ValueError("Empty Notion URL/ID")
    s = s.split("?")[0]
    matches = UUID_RE.findall(s)
    if not matches:
        raise ValueError(f"Could not find a Notion UUID in: {url_or_id!r}")
    raw = matches[-1].replace("-", "").lower()
    if len(raw) != 32:
        raise ValueError(f"Invalid Notion UUID length in: {url_or_id!r}")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


# ---------------------------------------------------------------------------
# State (idempotency)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("State file corrupted, starting fresh", "WARN")
    return {
        "parent_id": None,
        "folder_ids": {},
        "migrated_files": {},
        "drive_attachments": {},
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Markdown cleanup
# ---------------------------------------------------------------------------

ESCAPE_RE = re.compile(r"\\([#*\[\]()_`!\\><\-+.])")
TRAILING_DOUBLE_SPACE_RE = re.compile(r"  +$", re.MULTILINE)
ATTACHMENT_LINK_RE = re.compile(r"!\[([^\]]*)\]\((images|attachments)/([^)]+)\)")
PLAIN_ATTACHMENT_RE = re.compile(r"\[([^\]]*)\]\((images|attachments)/([^)]+)\)")


def clean_icloud_md(content: str) -> str:
    content = ESCAPE_RE.sub(r"\1", content)
    content = TRAILING_DOUBLE_SPACE_RE.sub("", content)
    return content


def replace_attachments(content: str, attachment_map: dict) -> str:
    def repl(m):
        alt = m.group(1)
        path = m.group(3)
        fname = path.split("/")[-1]
        drive_id = attachment_map.get(fname)
        label = alt or fname
        if drive_id:
            url = f"https://drive.google.com/file/d/{drive_id}/view"
            return f"📎 [{label}]({url})"
        return f"📎 {label} _(allegato non trovato su Drive)_"

    content = ATTACHMENT_LINK_RE.sub(repl, content)
    content = PLAIN_ATTACHMENT_RE.sub(repl, content)
    return content


# ---------------------------------------------------------------------------
# Drive attachments index
# ---------------------------------------------------------------------------

def build_attachment_map_via_api(state: dict) -> dict:
    cached = state.get("drive_attachments", {})
    if cached:
        log(f"Using cached attachment map ({len(cached)} entries)")
        return cached

    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        log("Google API libs not installed; skipping Drive attachment lookup", "WARN")
        log("Install: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib", "WARN")
        return {}

    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
    here = Path(__file__).parent
    creds_path = here / "credentials.json"
    token_path = here / "token.json"

    if not creds_path.exists():
        log(f"credentials.json not found at {creds_path}; skipping Drive lookup", "WARN")
        return {}

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    log("Indexing Drive files for attachment lookup...")
    file_map = {}

    def list_children(parent_id):
        out = []
        token = None
        while True:
            resp = service.files().list(
                q=f"'{parent_id}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                pageToken=token,
            ).execute()
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return out

    visited = set()
    stack = [DRIVE_ROOT_FOLDER_ID]
    while stack:
        folder_id = stack.pop()
        if folder_id in visited:
            continue
        visited.add(folder_id)
        try:
            children = list_children(folder_id)
        except Exception as e:
            log(f"Drive list failed for {folder_id}: {e}", "WARN")
            continue
        for child in children:
            if child["mimeType"] == "application/vnd.google-apps.folder":
                stack.append(child["id"])
            else:
                file_map[child["name"]] = child["id"]

    log(f"Indexed {len(file_map)} Drive files")
    state["drive_attachments"] = file_map
    save_state(state)
    return file_map


# ---------------------------------------------------------------------------
# Notion API
# ---------------------------------------------------------------------------

import urllib.request
import urllib.error

NOTION_API = "https://api.notion.com/v1"


def notion_request(method, path, body=None):
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN env var not set")
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(NOTION_API + path, data=data, headers=headers, method=method)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            if e.code == 429:
                wait = 2 ** attempt
                log(f"Rate limited, sleeping {wait}s", "WARN")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Notion API {method} {path} -> {e.code}: {text}") from e
        except urllib.error.URLError as e:
            wait = 2 ** attempt
            log(f"Network error: {e}, retrying in {wait}s", "WARN")
            time.sleep(wait)
    raise RuntimeError(f"Notion API {method} {path} failed after retries")


def notion_check_access(parent_id):
    """Sanity-check that the integration can read the parent page."""
    log(f"Verifying Notion integration access to parent page {parent_id}...")
    try:
        resp = notion_request("GET", f"/pages/{parent_id}")
    except RuntimeError:
        log("Cannot access parent page. The integration likely isn't connected.", "ERROR")
        log("Open the page in Notion -> ... -> Connections -> add your integration.", "ERROR")
        raise
    title = ""
    props = resp.get("properties", {})
    title_prop = props.get("title")
    if title_prop is None:
        title_prop = next((v for v in props.values() if v.get("type") == "title"), None)
    if title_prop and title_prop.get("title"):
        title = "".join(t.get("plain_text", "") for t in title_prop["title"])
    log(f"  OK: parent page accessible: '{title or '(untitled)'}'")


def md_to_notion_blocks(content):
    blocks = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].rstrip()

        if stripped.startswith("```"):
            lang = stripped[3:].strip() or "plain text"
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].rstrip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(code_lines)
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": code[:2000]}}],
                    "language": _normalize_lang(lang),
                },
            })
            continue

        m = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2)
            blocks.append({
                "object": "block",
                "type": f"heading_{level}",
                f"heading_{level}": {
                    "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]
                },
            })
            i += 1
            continue

        m = re.match(r"^[-*+]\s+(.+)$", stripped)
        if m:
            text = m.group(1)
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]
                },
            })
            i += 1
            continue

        m = re.match(r"^\d+\.\s+(.+)$", stripped)
        if m:
            text = m.group(1)
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {
                    "rich_text": [{"type": "text", "text": {"content": text[:2000]}}]
                },
            })
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].rstrip()
            if not nxt:
                break
            if re.match(r"^(#{1,3})\s+", nxt) or nxt.startswith("```"):
                break
            if re.match(r"^[-*+]\s+", nxt) or re.match(r"^\d+\.\s+", nxt):
                break
            para_lines.append(nxt)
            i += 1
        para_text = "\n".join(para_lines)
        for chunk in _split_chunks(para_text, 2000):
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                },
            })

    return blocks


def _split_chunks(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _normalize_lang(lang):
    lang = lang.lower().strip()
    aliases = {
        "js": "javascript", "ts": "typescript", "py": "python", "sh": "shell",
        "rb": "ruby", "yml": "yaml", "md": "markdown",
    }
    valid = {
        "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript",
        "c++", "c#", "css", "dart", "diff", "docker", "elixir", "elm",
        "erlang", "flow", "fortran", "f#", "gherkin", "glsl", "go",
        "graphql", "groovy", "haskell", "html", "java", "javascript", "json",
        "julia", "kotlin", "latex", "less", "lisp", "livescript", "lua",
        "makefile", "markdown", "markup", "matlab", "mermaid", "nix",
        "objective-c", "ocaml", "pascal", "perl", "php", "plain text",
        "powershell", "prolog", "protobuf", "python", "r", "reason", "ruby",
        "rust", "sass", "scala", "scheme", "scss", "shell", "solidity",
        "sql", "swift", "typescript", "vb.net", "verilog", "vhdl",
        "visual basic", "webassembly", "xml", "yaml",
    }
    lang = aliases.get(lang, lang)
    return lang if lang in valid else "plain text"


def notion_create_page(parent_page_id, title, blocks):
    initial = blocks[:95]
    rest = blocks[95:]
    body = {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": title[:2000]}}]}
        },
        "children": initial,
    }
    resp = notion_request("POST", "/pages", body)
    page_id = resp["id"]
    while rest:
        chunk = rest[:95]
        rest = rest[95:]
        notion_request("PATCH", f"/blocks/{page_id}/children", {"children": chunk})
        time.sleep(0.4)
    time.sleep(0.4)
    return page_id


def notion_get_existing_children(parent_page_id):
    """Return {title: page_id} of direct child pages."""
    out = {}
    cursor = None
    while True:
        path = f"/blocks/{parent_page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        resp = notion_request("GET", path)
        for block in resp.get("results", []):
            if block.get("type") == "child_page":
                title = block["child_page"].get("title", "")
                out[title] = block["id"]
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return out


# ---------------------------------------------------------------------------
# Note discovery & filtering
# ---------------------------------------------------------------------------

DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def title_from_filename(filename):
    stem = Path(filename).stem
    return DATE_PREFIX_RE.sub("", stem)


def should_skip_note(title, content):
    for pat in SKIP_TITLE_PATTERNS:
        if pat.match(title):
            return True, f"title matches skip pattern: {title}"
    cleaned = clean_icloud_md(content).strip()
    lines = cleaned.split("\n", 1)
    body = lines[1] if len(lines) > 1 else ""
    if len(body.strip()) < MIN_CONTENT_CHARS:
        return True, f"body too short ({len(body.strip())} chars)"
    return False, ""


def discover_notes(notes_root):
    out = {}
    if not notes_root.exists():
        log(f"NOTES_ROOT does not exist: {notes_root}", "ERROR")
        sys.exit(1)
    for top in sorted(notes_root.iterdir()):
        if not top.is_dir():
            continue
        if top.name in SKIP_FOLDERS:
            log(f"Skipping folder: {top.name}")
            continue
        mds = []
        for path in top.rglob("*.md"):
            if any(part in SKIP_FOLDERS for part in path.relative_to(top).parts[:-1]):
                continue
            mds.append(path)
        if mds:
            out[top.name] = mds
    return out


def normalize_folder_name(name):
    return name.strip().lower()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def ensure_folder_page(folder_name, parent_id, state, dry_run, top_level_existing):
    if folder_name in state["folder_ids"]:
        return state["folder_ids"][folder_name]

    norm = normalize_folder_name(folder_name)
    for existing_title, existing_id in top_level_existing.items():
        if normalize_folder_name(existing_title) == norm:
            log(f"  Reusing existing Notion folder page: '{existing_title}' (matched local '{folder_name}')")
            state["folder_ids"][folder_name] = existing_id
            save_state(state)
            return existing_id

    log(f"Creating top-level folder page: {folder_name}")
    if dry_run:
        return f"<would-create-{folder_name}>"
    body = {
        "parent": {"page_id": parent_id},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": folder_name}}]}
        },
    }
    resp = notion_request("POST", "/pages", body)
    pid = resp["id"]
    state["folder_ids"][folder_name] = pid
    save_state(state)
    time.sleep(0.4)
    return pid


def migrate_note(md_path, folder_page_id, state, attachment_map, existing_titles, dry_run):
    abspath = str(md_path.resolve())
    if abspath in state["migrated_files"]:
        return "skip-already-migrated"

    title = title_from_filename(md_path.name)
    if title in existing_titles:
        state["migrated_files"][abspath] = existing_titles[title]
        save_state(state)
        return "skip-already-on-notion"

    raw = md_path.read_text(encoding="utf-8", errors="replace")
    skip, reason = should_skip_note(title, raw)
    if skip:
        return f"skip-{reason}"

    cleaned = clean_icloud_md(raw)
    lines = cleaned.split("\n", 1)
    if lines and re.match(r"^#\s+", lines[0]):
        cleaned = lines[1] if len(lines) > 1 else ""
    cleaned = cleaned.lstrip("\n")
    cleaned = replace_attachments(cleaned, attachment_map)

    blocks = md_to_notion_blocks(cleaned)
    if not blocks:
        return "skip-no-blocks"

    if dry_run:
        log(f"  [dry-run] Would create: {title} ({len(blocks)} blocks)")
        return "dry-run"

    try:
        page_id = notion_create_page(folder_page_id, title, blocks)
        state["migrated_files"][abspath] = page_id
        save_state(state)
        return f"created:{page_id}"
    except Exception as e:
        log(f"  Failed to create '{title}': {e}", "ERROR")
        return f"error:{e}"


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--parent-url",
        help="URL (or raw ID) of the Notion page that holds the top-level folders. "
             "Can also be set via NOTION_NOTES_PARENT_URL env var.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    parser.add_argument("--folder", help="Only process this top-level folder name")
    parser.add_argument("--no-drive-api", action="store_true", help="Skip Drive API attachment lookup")
    parser.add_argument("--reset-state", action="store_true", help="Delete state file and start fresh")
    args = parser.parse_args()

    parent_url_or_id = args.parent_url or DEFAULT_PARENT_URL
    if not parent_url_or_id:
        log("Missing parent page. Pass --parent-url 'https://www.notion.so/...' "
            "or set NOTION_NOTES_PARENT_URL env var.", "ERROR")
        return 1
    try:
        parent_id = extract_notion_id(parent_url_or_id)
    except ValueError as e:
        log(f"Invalid parent URL/ID: {e}", "ERROR")
        return 1

    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()
        log("State file deleted")

    if not NOTION_TOKEN and not args.dry_run:
        log("NOTION_TOKEN env var is required (use --dry-run to preview)", "ERROR")
        return 1

    log(f"NOTES_ROOT = {NOTES_ROOT}")
    log(f"Parent page = {parent_id}  (from: {parent_url_or_id})")
    log(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")

    state = load_state()
    if state.get("parent_id") and state["parent_id"] != parent_id:
        log(f"Parent changed (was {state['parent_id']}, now {parent_id}); resetting folder cache", "WARN")
        state["folder_ids"] = {}
    state["parent_id"] = parent_id
    save_state(state)

    if not args.dry_run:
        try:
            notion_check_access(parent_id)
        except RuntimeError:
            return 1

    notes_by_folder = discover_notes(NOTES_ROOT)
    if args.folder:
        if args.folder not in notes_by_folder:
            log(f"Folder '{args.folder}' not found. Available: {sorted(notes_by_folder)}", "ERROR")
            return 1
        notes_by_folder = {args.folder: notes_by_folder[args.folder]}

    total = sum(len(v) for v in notes_by_folder.values())
    log(f"Found {total} .md files across {len(notes_by_folder)} folders")

    if args.dry_run:
        top_level_existing = {}
    else:
        top_level_existing = notion_get_existing_children(parent_id)
        log(f"Parent already contains {len(top_level_existing)} top-level pages")

    if args.no_drive_api:
        attachment_map = {}
    else:
        attachment_map = build_attachment_map_via_api(state)

    summary = {"created": 0, "skipped": 0, "errors": 0}
    for folder_name, md_files in sorted(notes_by_folder.items()):
        log(f"=== Folder: {folder_name} ({len(md_files)} files) ===")
        folder_pid = ensure_folder_page(folder_name, parent_id, state, args.dry_run, top_level_existing)

        if not args.dry_run:
            existing_titles = notion_get_existing_children(folder_pid)
            log(f"  {len(existing_titles)} pages already on Notion in this folder")
        else:
            existing_titles = {}

        for md_path in sorted(md_files, key=lambda p: p.name):
            result = migrate_note(md_path, folder_pid, state, attachment_map, existing_titles, args.dry_run)
            short_name = md_path.name
            if result.startswith("created:"):
                summary["created"] += 1
                log(f"  + {short_name}")
                existing_titles[title_from_filename(short_name)] = result.split(":", 1)[1]
            elif result.startswith("error:"):
                summary["errors"] += 1
                log(f"  ! {short_name} -- {result}", "ERROR")
            elif result.startswith("skip"):
                summary["skipped"] += 1
                log(f"  . {short_name} ({result})")
            elif result == "dry-run":
                summary["created"] += 1
            time.sleep(0.35)

    log("")
    log("=== SUMMARY ===")
    log(f"Created: {summary['created']}")
    log(f"Skipped: {summary['skipped']}")
    log(f"Errors:  {summary['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
