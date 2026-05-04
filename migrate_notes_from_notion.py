#!/usr/bin/env python3
"""
Export a Notion page tree to a local folder of .md files.

Mirror of migrate_notes_to_notion.py, in the opposite direction:
- Walk the page tree starting from --parent-url
- For each page that has child pages -> create a folder
- For each page write a .md file with the page content
- Pages that have BOTH content and child pages get an "index.md" inside the folder

PREREQUISITES:
    No external libraries required (pure stdlib).

USAGE:
    export NOTION_TOKEN="secret_..."
    python3 migrate_notes_from_notion.py --parent-url "https://www.notion.so/..."
    python3 migrate_notes_from_notion.py --parent-url "..." --export ./output/
    python3 migrate_notes_from_notion.py --parent-url "..." --dry-run
    python3 migrate_notes_from_notion.py --parent-url "..." --max-depth 3

REQUIRED NOTION PERMISSIONS:
    The integration whose token is in NOTION_TOKEN must have access to the
    parent page. Open it in Notion -> ... -> Connections -> add your integration.
    Sub-pages inherit access automatically.

OUTPUT FORMAT:
    Each .md file starts with a YAML-style frontmatter block:
      ---
      title: Original page title
      source: https://www.notion.so/...
      notion_id: <page-uuid>
      created: 2024-01-01T...
      last_edited: 2024-...
      ---
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DEFAULT_PARENT_URL = os.environ.get("NOTION_NOTES_PARENT_URL", "")

DEFAULT_EXPORT_DIR = Path(__file__).parent / "export"
STATE_FILE = Path(__file__).parent / ".export_state.json"
LOG_FILE = Path(__file__).parent / "export.log"

NOTION_API = "https://api.notion.com/v1"
RATE_LIMIT_SLEEP = 0.35  # seconds between calls

# Filename sanitization
INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg, level="INFO"):
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


def extract_notion_id(url_or_id):
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
# State
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("State file corrupted, starting fresh", "WARN")
    return {
        "parent_id": None,
        "exported_pages": {},  # page_id -> output file path (relative)
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Notion API
# ---------------------------------------------------------------------------

def notion_request(method, path, body=None, *, max_retries=5):
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN env var not set")
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(NOTION_API + path, data=data, headers=headers, method=method)
    for attempt in range(max_retries):
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


def notion_get_page(page_id):
    return notion_request("GET", f"/pages/{page_id}")


def notion_get_block_children(block_id):
    out = []
    cursor = None
    while True:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        resp = notion_request("GET", path)
        out.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return out


def notion_check_access(page_id):
    log(f"Verifying Notion integration access to page {page_id}...")
    try:
        resp = notion_get_page(page_id)
    except RuntimeError:
        log("Cannot access page. The integration likely isn't connected.", "ERROR")
        log("Open the page in Notion -> ... -> Connections -> add your integration.", "ERROR")
        raise
    title = extract_page_title(resp) or "(untitled)"
    log(f"  OK: page accessible: '{title}'")
    return resp


# ---------------------------------------------------------------------------
# Notion -> Markdown converters
# ---------------------------------------------------------------------------

def extract_page_title(page):
    """Return the title of a page object as plain text."""
    props = page.get("properties", {})
    title_prop = props.get("title")
    if title_prop is None:
        title_prop = next((v for v in props.values() if v.get("type") == "title"), None)
    if not title_prop:
        return ""
    return rich_text_to_plain(title_prop.get("title", []))


def rich_text_to_plain(rich_text):
    """Concatenate rich_text segments into plain text (no formatting)."""
    return "".join(rt.get("plain_text", "") for rt in rich_text or [])


def rich_text_to_markdown(rich_text):
    """Convert a list of rich_text segments back into markdown inline syntax."""
    out = []
    for rt in rich_text or []:
        text = rt.get("plain_text", "")
        if not text:
            continue
        ann = rt.get("annotations", {}) or {}
        href = (rt.get("text") or {}).get("link") or rt.get("href")
        url = None
        if href:
            url = href.get("url") if isinstance(href, dict) else href
        # Apply inline formatting (order matters; code wraps everything else)
        if ann.get("code"):
            text = f"`{text}`"
        else:
            if ann.get("bold") and ann.get("italic"):
                text = f"***{text}***"
            elif ann.get("bold"):
                text = f"**{text}**"
            elif ann.get("italic"):
                text = f"*{text}*"
            if ann.get("strikethrough"):
                text = f"~~{text}~~"
        if url:
            # If the text equals the URL, emit a single autolink-style instead of [url](url)
            if text == url:
                text = url
            else:
                text = f"[{text}]({url})"
        out.append(text)
    return "".join(out)


def block_to_markdown(block, depth=0, *, indent_str="  "):
    """Convert a Notion block to one or more lines of Markdown.

    Returns a list of strings (lines).
    Tables and code already include their fences/structure inline.
    Indent is applied for nested list items.
    """
    btype = block.get("type")
    payload = block.get(btype, {}) or {}
    indent = indent_str * depth

    if btype == "paragraph":
        text = rich_text_to_markdown(payload.get("rich_text", []))
        return [indent + text] if text else [""]

    if btype in ("heading_1", "heading_2", "heading_3"):
        level = int(btype.split("_")[1])
        text = rich_text_to_markdown(payload.get("rich_text", []))
        return [f"{'#' * level} {text}"] if text else []

    if btype == "bulleted_list_item":
        text = rich_text_to_markdown(payload.get("rich_text", []))
        return [f"{indent}- {text}"]

    if btype == "numbered_list_item":
        # We can't track numbering across siblings easily; "1." is fine since
        # markdown renderers auto-renumber.
        text = rich_text_to_markdown(payload.get("rich_text", []))
        return [f"{indent}1. {text}"]

    if btype == "to_do":
        text = rich_text_to_markdown(payload.get("rich_text", []))
        checked = "x" if payload.get("checked") else " "
        return [f"{indent}- [{checked}] {text}"]

    if btype == "toggle":
        text = rich_text_to_markdown(payload.get("rich_text", []))
        # Toggle has no native markdown; render as a bullet (children handled by caller)
        return [f"{indent}- {text}" if text else f"{indent}-"]

    if btype == "quote":
        text = rich_text_to_markdown(payload.get("rich_text", []))
        # Multi-line quotes become each line prefixed with >
        return [f"> {ln}" for ln in (text.split("\n") or [""])]

    if btype == "callout":
        emoji = ""
        icon = payload.get("icon")
        if icon and icon.get("type") == "emoji":
            emoji = icon.get("emoji", "") + " "
        text = rich_text_to_markdown(payload.get("rich_text", []))
        return [f"> {emoji}{text}"] if (emoji or text) else []

    if btype == "code":
        text = rich_text_to_plain(payload.get("rich_text", []))
        lang = (payload.get("language") or "").strip()
        if lang in ("plain text", "plaintext"):
            lang = ""
        out = [f"```{lang}"]
        out.extend(text.split("\n"))
        out.append("```")
        return out

    if btype == "divider":
        return ["---"]

    if btype == "image":
        return [_image_block_to_md(payload)]

    if btype == "file":
        return [_file_block_to_md(payload, "📎 ")]

    if btype == "video":
        return [_file_block_to_md(payload, "🎬 ")]

    if btype == "audio":
        return [_file_block_to_md(payload, "🎵 ")]

    if btype == "pdf":
        return [_file_block_to_md(payload, "📄 ")]

    if btype == "bookmark":
        url = payload.get("url", "")
        caption = rich_text_to_markdown(payload.get("caption", []))
        if caption:
            return [f"🔖 [{caption}]({url})"]
        return [f"🔖 {url}"]

    if btype == "embed":
        url = payload.get("url", "")
        return [f"🔗 {url}"] if url else []

    if btype == "equation":
        expr = payload.get("expression", "")
        return [f"$${expr}$$"] if expr else []

    if btype == "table":
        # Table contents are in children (see caller)
        return ["__TABLE_PLACEHOLDER__"]

    if btype == "child_page":
        # Handled by the page walker, not here
        return []

    if btype == "child_database":
        title = payload.get("title", "")
        return [f"_(database: {title})_"]

    if btype == "synced_block":
        # Render children inline if available
        return []

    # Unknown block types: emit a placeholder comment
    return [f"<!-- unsupported block type: {btype} -->"]


def _image_block_to_md(payload):
    src = ""
    if payload.get("type") == "external":
        src = payload.get("external", {}).get("url", "")
    elif payload.get("type") == "file":
        src = payload.get("file", {}).get("url", "")
    else:
        src = (payload.get("external") or {}).get("url") or (payload.get("file") or {}).get("url") or ""
    caption = rich_text_to_markdown(payload.get("caption", []))
    alt = caption or "image"
    return f"![{alt}]({src})"


def _file_block_to_md(payload, prefix):
    src = ""
    name = payload.get("name", "")
    if payload.get("type") == "external":
        src = payload.get("external", {}).get("url", "")
    elif payload.get("type") == "file":
        src = payload.get("file", {}).get("url", "")
    label = name or src or "file"
    if src:
        return f"{prefix}[{label}]({src})"
    return f"{prefix}{label}"


def render_table(table_block, rows):
    """Build a markdown pipe-table from a Notion table block + its row children."""
    n_cols = table_block.get("table", {}).get("table_width", 0) or 1
    has_header = table_block.get("table", {}).get("has_column_header", False)

    md_rows = []
    for r in rows:
        if r.get("type") != "table_row":
            continue
        cells = r.get("table_row", {}).get("cells", [])
        # Each cell is a list of rich_text -> render to markdown, then escape pipes
        md_cells = []
        for c in cells:
            text = rich_text_to_markdown(c).replace("|", r"\|").replace("\n", " ")
            md_cells.append(text)
        # Pad to n_cols
        md_cells = (md_cells + [""] * n_cols)[:n_cols]
        md_rows.append("| " + " | ".join(md_cells) + " |")

    if not md_rows:
        return []

    out = []
    if has_header:
        out.append(md_rows[0])
        out.append("|" + "|".join(["---"] * n_cols) + "|")
        out.extend(md_rows[1:])
    else:
        # No header row: synthesize one from blank cells
        out.append("|" + "|".join([" "] * n_cols) + "|")
        out.append("|" + "|".join(["---"] * n_cols) + "|")
        out.extend(md_rows)
    return out


# ---------------------------------------------------------------------------
# Block tree walk -> markdown
# ---------------------------------------------------------------------------

def walk_blocks_to_md(block_id, depth=0, *, child_pages_collected=None):
    """Recursively fetch and convert a block subtree to markdown lines.

    child_pages_collected: optional list[str] (block ids of child_page blocks)
                           to pass back to the caller for further processing.
    Returns: list of markdown line strings.
    """
    lines = []
    children = notion_get_block_children(block_id)
    time.sleep(RATE_LIMIT_SLEEP)

    i = 0
    while i < len(children):
        block = children[i]
        btype = block.get("type")

        # Track child pages (we'll process them as separate files at the caller level)
        if btype == "child_page":
            if child_pages_collected is not None:
                child_pages_collected.append(block["id"])
            i += 1
            continue

        if btype == "table":
            # Collect all table_row children of THIS table block
            table_rows = notion_get_block_children(block["id"])
            time.sleep(RATE_LIMIT_SLEEP)
            md = render_table(block, table_rows)
            lines.extend(md)
            lines.append("")  # blank line after table
            i += 1
            continue

        # Convert single block
        md_lines = block_to_markdown(block, depth=depth)
        lines.extend(md_lines)

        # Recurse into children for nested constructs
        if block.get("has_children") and btype not in ("child_page", "child_database", "table"):
            # For list items / toggles, indent children one level deeper
            child_depth = depth
            if btype in ("bulleted_list_item", "numbered_list_item", "to_do", "toggle"):
                child_depth = depth + 1
            child_lines = walk_blocks_to_md(
                block["id"],
                depth=child_depth,
                child_pages_collected=child_pages_collected,
            )
            lines.extend(child_lines)

        # Add blank line after block-level constructs that aren't list items
        if btype in (
            "paragraph", "heading_1", "heading_2", "heading_3", "code",
            "quote", "callout", "divider", "image", "video", "audio",
            "file", "pdf", "bookmark", "embed", "equation",
        ):
            lines.append("")

        i += 1

    return lines


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def safe_filename(name, fallback="untitled"):
    """Make a string safe for use as a filename or folder name (cross-platform)."""
    if not name:
        return fallback
    s = INVALID_FS_CHARS.sub(" ", name)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")  # Windows doesn't allow trailing dots/spaces
    if not s:
        return fallback
    # Limit length (255 is FS max but be conservative for combined paths)
    return s[:120]


def unique_path(path):
    """If path exists, append -2, -3, ... until unique."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    n = 2
    while True:
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def build_frontmatter(page):
    """Build a YAML-style frontmatter from a Notion page object."""
    title = extract_page_title(page)
    notion_id = page.get("id", "")
    url = page.get("url", "")
    created = page.get("created_time", "")
    last_edited = page.get("last_edited_time", "")
    return "\n".join([
        "---",
        f"title: {_yaml_escape(title)}",
        f"source: {url}",
        f"notion_id: {notion_id}",
        f"created: {created}",
        f"last_edited: {last_edited}",
        "---",
        "",
    ])


def _yaml_escape(s):
    """Minimal YAML escape: quote if string contains characters that would break a bare scalar."""
    if not s:
        return '""'
    if any(c in s for c in [":", "#", "\n", "'", '"', "\\", "{", "}", "[", "]", ","]) or s.strip() != s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


# ---------------------------------------------------------------------------
# Page tree walk -> filesystem
# ---------------------------------------------------------------------------

def export_page(page_id, target_dir, state, dry_run, current_depth, max_depth):
    """Export a single page (and recurse into its children).

    target_dir: where to place the .md file (and the folder, if it has children)
    Returns the relative path to the .md created (or None if nothing was written).
    """
    page = notion_get_page(page_id)
    time.sleep(RATE_LIMIT_SLEEP)

    title = extract_page_title(page) or "untitled"
    safe_name = safe_filename(title)

    log(f"{'  ' * current_depth}* {title}")

    # Walk blocks; collect child_page IDs separately
    child_page_ids = []
    body_lines = walk_blocks_to_md(page_id, child_pages_collected=child_page_ids)

    # Strip trailing blank lines
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    body = "\n".join(body_lines).rstrip() + "\n" if body_lines else ""

    has_body = bool(body.strip())
    has_children = bool(child_page_ids)

    # Decide layout for this page:
    #   - leaf (no child pages):                target_dir/<name>.md
    #   - has children, no body:                target_dir/<name>/  (no index.md)
    #   - has children AND body:                target_dir/<name>/index.md
    if has_children:
        folder = target_dir / safe_name
        if not dry_run:
            folder.mkdir(parents=True, exist_ok=True)
        if has_body:
            md_path = folder / "index.md"
        else:
            md_path = None
    else:
        md_path = target_dir / f"{safe_name}.md"

    if md_path is not None:
        if not dry_run:
            md_path = unique_path(md_path)
            md_path.parent.mkdir(parents=True, exist_ok=True)
            content = build_frontmatter(page) + body
            md_path.write_text(content, encoding="utf-8")
        rel_path = str(md_path)
        state["exported_pages"][page_id] = rel_path
        save_state(state)

    # Recurse into children
    if has_children:
        if max_depth is not None and current_depth >= max_depth:
            log(f"{'  ' * (current_depth + 1)}(max-depth reached, skipping {len(child_page_ids)} children)", "WARN")
        else:
            child_target = target_dir / safe_name  # folder we created above
            for cid in child_page_ids:
                # Skip if we've already exported this page id (idempotency)
                if cid in state["exported_pages"]:
                    log(f"{'  ' * (current_depth + 1)}. (skipped, already exported: {state['exported_pages'][cid]})")
                    continue
                try:
                    export_page(cid, child_target, state, dry_run, current_depth + 1, max_depth)
                except Exception as e:
                    log(f"{'  ' * (current_depth + 1)}! failed to export {cid}: {e}", "ERROR")

    return md_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--parent-url",
        help="URL or ID of the Notion page to start exporting from. "
             "Can also be set via NOTION_NOTES_PARENT_URL env var.",
    )
    parser.add_argument(
        "--export",
        default=str(DEFAULT_EXPORT_DIR),
        help="Output directory (default: ./export/)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum recursion depth (default: unlimited)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't write files")
    parser.add_argument("--reset-state", action="store_true", help="Delete state file and start fresh")
    parser.add_argument(
        "--include-root",
        action="store_true",
        help="Also export the parent page itself as <export>/<parent-title>.md "
             "(by default we only export its children).",
    )
    args = parser.parse_args()

    parent_url_or_id = args.parent_url or DEFAULT_PARENT_URL
    if not parent_url_or_id:
        log("Missing parent page. Pass --parent-url or set NOTION_NOTES_PARENT_URL", "ERROR")
        return 1
    try:
        parent_id = extract_notion_id(parent_url_or_id)
    except ValueError as e:
        log(f"Invalid parent URL/ID: {e}", "ERROR")
        return 1

    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()
        log("State file deleted")

    if not NOTION_TOKEN:
        log("NOTION_TOKEN env var is required", "ERROR")
        return 1

    export_dir = Path(args.export).expanduser().resolve()

    log(f"Export dir = {export_dir}")
    log(f"Parent page = {parent_id}  (from: {parent_url_or_id})")
    log(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    if args.max_depth is not None:
        log(f"Max depth = {args.max_depth}")

    state = load_state()
    if state.get("parent_id") != parent_id:
        if state.get("exported_pages"):
            log(f"Parent changed (was {state.get('parent_id')!r}, now {parent_id!r}); "
                f"resetting state cache", "WARN")
        state["exported_pages"] = {}
    state["parent_id"] = parent_id
    save_state(state)

    try:
        parent_page = notion_check_access(parent_id)
    except RuntimeError:
        return 1

    if not args.dry_run:
        export_dir.mkdir(parents=True, exist_ok=True)

    if args.include_root:
        # Treat the parent like any other page: export its content + its children
        log("=== Exporting parent page + descendants ===")
        export_page(parent_id, export_dir, state, args.dry_run, current_depth=0, max_depth=args.max_depth)
    else:
        # Default: skip the parent's own content, just walk its children into export_dir
        log("=== Exporting descendants of parent page ===")
        # Get child page ids from the parent
        child_page_ids = []
        for block in notion_get_block_children(parent_id):
            if block.get("type") == "child_page":
                child_page_ids.append(block["id"])
        time.sleep(RATE_LIMIT_SLEEP)
        log(f"Found {len(child_page_ids)} top-level child page{'s' if len(child_page_ids) != 1 else ''} under parent")

        for cid in child_page_ids:
            if cid in state["exported_pages"]:
                log(f"  . skipped (already exported: {state['exported_pages'][cid]})")
                continue
            try:
                export_page(cid, export_dir, state, args.dry_run, current_depth=0, max_depth=args.max_depth)
            except Exception as e:
                log(f"  ! failed to export {cid}: {e}", "ERROR")

    log("")
    log("=== SUMMARY ===")
    log(f"Pages exported: {len(state['exported_pages'])}")
    log(f"Output: {export_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
