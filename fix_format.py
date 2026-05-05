#!/usr/bin/env python3
"""
Fix common formatting issues in already-migrated Notion pages.

Walks the Notion page tree starting from --parent-url (or processes a single
page via --url) and applies these fixups to every block with rich_text:

  1. <u>...</u>   ->  underline annotation
  2. ~~...~~ (markdown)  ->  strikethrough annotation
  3. [email@domain](mailto:email@domain)  ->  proper mailto: link
  4. Blocks that contain only "##" (or other heading markers as plain text)
     are deleted (these are stray heading-marker leftovers from iCloud export)
  5. Code blocks: remove extra blank lines introduced during migration
  6. Consecutive code blocks under the same parent are merged into one
     (contents joined with a single newline)

USAGE:
    export NOTION_TOKEN="secret_..."

    # Whole tree under a parent page
    python3 fix_format.py --parent-url "https://www.notion.so/..."

    # Single page only
    python3 fix_format.py --url "https://www.notion.so/some-page-12345..."

    # Dry run (preview changes, write nothing)
    python3 fix_format.py --parent-url "..." --dry-run

    # Skip individual fixes if needed
    python3 fix_format.py --parent-url "..." --no-underline --no-mailto
    python3 fix_format.py --parent-url "..." --no-strike-tilde

REQUIRED NOTION PERMISSIONS:
    The integration whose token is in NOTION_TOKEN must have access to the
    parent / target page. Open it in Notion -> ... -> Connections -> add
    your integration. Sub-pages inherit access automatically.

LIMITATIONS:
    - <u>...</u> is applied after merging adjacent text segments with matching
      bold/italic/code/color when links are equal or only one run has a link
      (common when Notion autolinks the URL in the middle run only).
      If the merged text would exceed 2000 characters per segment, or two
      different link URLs meet, tags may still be reported as unbalanced.
    - ``~~...~~`` must form complete pairs in the coalesced text; odd ``~~`` or
      pairs split by incompatible styling/links are left as-is and skipped.
    - The "delete stray heading marker" fix only matches blocks that consist
      ENTIRELY of "##", "###", "#", optionally surrounded by whitespace.
"""

import argparse
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DEFAULT_PARENT_URL = os.environ.get("NOTION_NOTES_PARENT_URL", "")
NOTION_API = "https://api.notion.com/v1"
RATE_LIMIT_SLEEP = 0.35

RICH_TEXT_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do", "toggle",
    "quote", "callout",
}

# --- patterns -------------------------------------------------------------

UNDERLINE_TAG_RE = re.compile(r"<u>(.*?)</u>", re.DOTALL | re.IGNORECASE)
UNBALANCED_TAG_RE = re.compile(r"<u>|</u>", re.IGNORECASE)

# Markdown strikethrough: ~~Green opaque~~ -> strikethrough annotation
STRIKETHROUGH_TILDE_RE = re.compile(r"~~(.*?)~~", re.DOTALL)

# Email at simple grammar: local-part@domain.tld - good enough for our notes
EMAIL_BASIC = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
# Pattern for the stale "[email](mailto:email)" or "[email](email)" markdown
# left as plain text. Group 1 = visible email, group 2 = target email.
MAILTO_LINK_RE = re.compile(
    r"\[(" + EMAIL_BASIC + r")\]\((?:mailto:)?(" + EMAIL_BASIC + r")\)"
)

# Pattern for a block whose text is only heading markers (and whitespace).
# Matches "##", " ## ", "###", "#", etc.
ONLY_HEADING_MARKERS_RE = re.compile(r"^\s*#{1,6}\s*$")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg, level="INFO"):
    print(f"[{time.strftime('%H:%M:%S')}] {level:5s} {msg}")


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

UUID_RE = re.compile(
    r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)


def extract_notion_id(url_or_id):
    s = url_or_id.strip().split("?")[0]
    matches = UUID_RE.findall(s)
    if not matches:
        raise ValueError(f"Could not find a Notion UUID in: {url_or_id!r}")
    raw = matches[-1].replace("-", "").lower()
    if len(raw) != 32:
        raise ValueError(f"Invalid Notion UUID length in: {url_or_id!r}")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


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


def get_block_children(block_id):
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


# ---------------------------------------------------------------------------
# Helpers for rich_text manipulation
# ---------------------------------------------------------------------------

def _make_text_segment(content, *, base_segment, override_link=None,
                       override_underline=None, override_strikethrough=None):
    """Clone the structure of a base rich_text segment with new content and
    optionally overridden link / underline / strikethrough annotation."""
    if not content:
        return None
    out = {
        "type": "text",
        "text": {"content": content[:2000]},
        "annotations": dict(base_segment.get("annotations") or {
            "bold": False, "italic": False, "strikethrough": False,
            "underline": False, "code": False, "color": "default",
        }),
    }
    base_link = (base_segment.get("text") or {}).get("link")
    if override_link is not None:
        if override_link:
            out["text"]["link"] = {"url": override_link}
    elif base_link:
        out["text"]["link"] = base_link
    if override_underline is not None:
        out["annotations"]["underline"] = override_underline
    if override_strikethrough is not None:
        out["annotations"]["strikethrough"] = override_strikethrough
    return out


def block_text(block):
    """Return concatenated plain text from a block's rich_text payload."""
    btype = block.get("type")
    if btype not in RICH_TEXT_BLOCK_TYPES:
        return ""
    rt = block.get(btype, {}).get("rich_text", [])
    return "".join((s.get("text") or {}).get("content", "") for s in rt)


def _link_url(seg):
    """HTTP(S) / mailto URL from a text segment, or None."""
    t = seg.get("text") or {}
    link = t.get("link")
    return (link or {}).get("url") if link else None


def _merge_style_key(seg):
    """Bold/italic/code/color only. Omit underline and strikethrough so runs
    split across ``<u>`` / ``~~`` / native flags still coalesce."""
    ann = seg.get("annotations") or {}
    return (
        bool(ann.get("bold")),
        bool(ann.get("italic")),
        bool(ann.get("code")),
        ann.get("color", "default"),
    )


def _compatible_link_merge(url_a, url_b):
    """Whether adjacent runs may merge; return resolved link URL for merged segment.

    Notion often puts ``https://...`` only on the middle run; neighbours are
    plain. Treat (None, x) as compatible and keep x.
    """
    if url_a == url_b:
        return True, url_a
    if not url_a:
        return True, url_b
    if not url_b:
        return True, url_a
    return False, None


def _copy_text_segment_structure(seg):
    """Deep copy of a text rich_text segment (API-shaped)."""
    t = seg.get("text") or {}
    out = {
        "type": "text",
        "text": {"content": t.get("content", "") or ""},
        "annotations": dict(
            seg.get("annotations")
            or {
                "bold": False,
                "italic": False,
                "strikethrough": False,
                "underline": False,
                "code": False,
                "color": "default",
            }
        ),
    }
    if t.get("link"):
        out["text"]["link"] = dict(t["link"])
    return out


def coalesce_adjacent_text_segments(rich_text):
    """Join consecutive text runs so literal ``<u>...</u>`` and ``~~..~~`` parse.

    Merges adjacent ``text`` segments when:
    - bold/italic/code/color match;
    - link URLs are equal, or exactly one side has a link (keep that URL);
    - combined length ≤ 2000 (Notion limit).

    ``underline`` / ``strikethrough`` may differ between runs; both are merged
    with OR onto the left run before marker stripping.
    """
    if not rich_text:
        return rich_text
    out = []
    for seg in rich_text:
        if seg.get("type") != "text":
            out.append(copy.deepcopy(seg))
            continue
        chunk = (seg.get("text") or {}).get("content", "") or ""
        if out and out[-1].get("type") == "text":
            prev_seg = out[-1]
            ok_links, merged_url = _compatible_link_merge(
                _link_url(prev_seg), _link_url(seg)
            )
            if ok_links and _merge_style_key(prev_seg) == _merge_style_key(seg):
                prev = (prev_seg.get("text") or {}).get("content", "") or ""
                if len(prev) + len(chunk) <= 2000:
                    prev_seg["text"]["content"] = prev + chunk
                    pa = prev_seg.get("annotations") or {}
                    sa = seg.get("annotations") or {}
                    ann_defaults = {
                        "bold": False,
                        "italic": False,
                        "strikethrough": False,
                        "underline": False,
                        "code": False,
                        "color": "default",
                    }
                    merged_ann = {**ann_defaults, **pa}
                    merged_ann["underline"] = bool(pa.get("underline")) or bool(
                        sa.get("underline")
                    )
                    merged_ann["strikethrough"] = bool(
                        pa.get("strikethrough")
                    ) or bool(sa.get("strikethrough"))
                    prev_seg["annotations"] = merged_ann
                    if merged_url:
                        prev_seg.setdefault("text", {})["link"] = {
                            "url": merged_url
                        }
                    else:
                        prev_seg.get("text", {}).pop("link", None)
                    continue
        out.append(_copy_text_segment_structure(seg))
    return out


# ---------------------------------------------------------------------------
# FIX 1: <u>...</u> -> underline annotation
# ---------------------------------------------------------------------------

def transform_underline(segment):
    """Return (new_segments, status). status: 'no_change' | 'replaced' | 'unbalanced'."""
    if segment.get("type") != "text":
        return [segment], "no_change"
    content = (segment.get("text") or {}).get("content", "")
    lower = content.lower()
    if "<u>" not in lower and "</u>" not in lower:
        return [segment], "no_change"

    out = []
    last = 0
    for m in UNDERLINE_TAG_RE.finditer(content):
        if m.start() > last:
            seg = _make_text_segment(content[last:m.start()], base_segment=segment)
            if seg:
                out.append(seg)
        inner = m.group(1)
        if inner:
            seg = _make_text_segment(inner, base_segment=segment, override_underline=True)
            if seg:
                out.append(seg)
        last = m.end()
    if last < len(content):
        seg = _make_text_segment(content[last:], base_segment=segment)
        if seg:
            out.append(seg)

    rebuilt = "".join((s.get("text") or {}).get("content", "") for s in out)
    if UNBALANCED_TAG_RE.search(rebuilt):
        return [segment], "unbalanced"
    return out, "replaced"


# ---------------------------------------------------------------------------
# ~~...~~ (markdown) -> strikethrough annotation
# ---------------------------------------------------------------------------

def transform_strikethrough_tilde(segment):
    """Return (new_segments, status). status: 'no_change' | 'replaced' | 'unbalanced'."""
    if segment.get("type") != "text":
        return [segment], "no_change"
    ann = segment.get("annotations", {}) or {}
    if (segment.get("text") or {}).get("link") or ann.get("code"):
        return [segment], "no_change"
    content = (segment.get("text") or {}).get("content", "")
    if "~~" not in content:
        return [segment], "no_change"

    out = []
    last = 0
    for m in STRIKETHROUGH_TILDE_RE.finditer(content):
        if m.start() > last:
            seg = _make_text_segment(content[last:m.start()], base_segment=segment)
            if seg:
                out.append(seg)
        inner = m.group(1)
        if inner:
            seg = _make_text_segment(
                inner,
                base_segment=segment,
                override_strikethrough=True,
            )
            if seg:
                out.append(seg)
        last = m.end()
    if last < len(content):
        seg = _make_text_segment(content[last:], base_segment=segment)
        if seg:
            out.append(seg)

    rebuilt = "".join((s.get("text") or {}).get("content", "") for s in out)
    if "~~" in rebuilt:
        return [segment], "unbalanced"
    return out, "replaced"


# ---------------------------------------------------------------------------
# FIX 2: [email](mailto:email) literal -> proper email link
# ---------------------------------------------------------------------------

def transform_mailto(segment):
    """Replace literal "[x@y.z](mailto:x@y.z)" patterns with linked segments."""
    if segment.get("type") != "text":
        return [segment], False
    # Don't alter segments that already have a link or are code
    ann = segment.get("annotations", {}) or {}
    if (segment.get("text") or {}).get("link") or ann.get("code"):
        return [segment], False
    content = (segment.get("text") or {}).get("content", "")
    if "](" not in content or "@" not in content:
        return [segment], False

    out = []
    last = 0
    changed = False
    for m in MAILTO_LINK_RE.finditer(content):
        if m.start() > last:
            seg = _make_text_segment(content[last:m.start()], base_segment=segment)
            if seg:
                out.append(seg)
        visible = m.group(1)
        target = m.group(2)
        seg = _make_text_segment(
            visible,
            base_segment=segment,
            override_link=f"mailto:{target}",
        )
        if seg:
            out.append(seg)
        changed = True
        last = m.end()
    if not changed:
        return [segment], False
    if last < len(content):
        seg = _make_text_segment(content[last:], base_segment=segment)
        if seg:
            out.append(seg)
    return out, True


# ---------------------------------------------------------------------------
# Combined rich_text transformer
# ---------------------------------------------------------------------------

def transform_rich_text(rich_text, *, do_underline=True, do_strike_tilde=True, do_mailto=True):
    """Apply all enabled rich_text fixes. Returns (new_rt, fix_counts, unbalanced)."""
    if not rich_text:
        return rich_text, {"underline": 0, "strikethrough": 0, "mailto": 0}, False
    rich_text = coalesce_adjacent_text_segments(rich_text)
    counts = {"underline": 0, "strikethrough": 0, "mailto": 0}
    unbalanced = False

    # Stage 1: underline
    if do_underline:
        staged = []
        for seg in rich_text:
            new_segs, status = transform_underline(seg)
            if status == "replaced":
                counts["underline"] += 1
            elif status == "unbalanced":
                unbalanced = True
            staged.extend(new_segs)
        rich_text = staged

    # Stage 1b: ~~strikethrough~~
    if do_strike_tilde:
        staged = []
        for seg in rich_text:
            new_segs, status = transform_strikethrough_tilde(seg)
            if status == "replaced":
                counts["strikethrough"] += 1
            elif status == "unbalanced":
                unbalanced = True
            staged.extend(new_segs)
        rich_text = staged

    # Stage 2: mailto links
    if do_mailto:
        staged = []
        for seg in rich_text:
            new_segs, changed = transform_mailto(seg)
            if changed:
                counts["mailto"] += 1
            staged.extend(new_segs)
        rich_text = staged

    return rich_text, counts, unbalanced


# ---------------------------------------------------------------------------
# FIX 3: remove extra blank lines inside code blocks
# ---------------------------------------------------------------------------

# Remove repeated newline runs inside code blocks. Notion stores code as plain
# text, so a visually extra blank line is represented by "\n\n".
EXTRA_CODE_NEWLINES_RE = re.compile(r"(?:[ \t]*(?:\r\n|\r|\n)){2,}")
NOTION_TEXT_CHUNK_SIZE = 2000


def _split_notion_text(content):
    return [
        {"type": "text", "text": {"content": content[i:i + NOTION_TEXT_CHUNK_SIZE]}}
        for i in range(0, len(content), NOTION_TEXT_CHUNK_SIZE)
    ]


def normalize_code_block_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = EXTRA_CODE_NEWLINES_RE.sub("\n", text)
    return text.strip("\n")


def transform_code_block(block):
    """Return (new_rich_text, changed) for a code block."""
    rt = block.get("code", {}).get("rich_text", [])
    if not rt:
        return rt, False

    text = "".join((s.get("text") or {}).get("content", "") for s in rt)
    new_text = normalize_code_block_text(text)

    if new_text == text:
        return rt, False

    # Rebuild as plain text segments without truncating Notion's 2000-char limit.
    return _split_notion_text(new_text), True


def code_block_plain_text(block):
    rt = block.get("code", {}).get("rich_text", [])
    return "".join((s.get("text") or {}).get("content", "") for s in rt)


def _consecutive_code_runs(children):
    """Indices of runs of 2+ adjacent code blocks (same parent)."""
    runs = []
    i = 0
    n = len(children)
    while i < n:
        if children[i].get("type") != "code":
            i += 1
            continue
        j = i
        while j + 1 < n and children[j + 1].get("type") == "code":
            j += 1
        if j > i:
            runs.append(children[i : j + 1])
        i = j + 1
    return runs


def _merge_code_language(run):
    langs = [b.get("code", {}).get("language") or "plain text" for b in run]
    first = langs[0]
    if all(l == first for l in langs):
        return first
    return "plain text"


def merge_consecutive_code_blocks(children, stats, dry_run, opts):
    """Merge adjacent code blocks via API; return ids to skip in the main loop."""
    if not opts.get("do_merge_code_blocks", True):
        return set()
    skip = set()
    for run in _consecutive_code_runs(children):
        if any(b.get("has_children") for b in run):
            log(
                f"  ! skip code merge (run of {len(run)}): a block has children",
                "WARN",
            )
            continue
        parts = [code_block_plain_text(b).rstrip("\r\n") for b in run]
        merged = "\n".join(parts)
        if opts.get("do_code_blanks", True):
            merged = normalize_code_block_text(merged)
        new_rt = _split_notion_text(merged)
        lang = _merge_code_language(run)
        first = run[0]
        absorbed = len(run) - 1
        stats["merged_code_blocks"] += absorbed
        log(
            f"  ~ merge {len(run)} code blocks -> {first['id']} "
            f"(absorbed {absorbed}, lang={lang!r})"
        )
        if dry_run:
            first.setdefault("code", {})["rich_text"] = new_rt
            first["code"]["language"] = lang
            skip.update(b["id"] for b in run[1:])
            continue
        try:
            update_code_block(first, new_rt, language=lang)
            time.sleep(RATE_LIMIT_SLEEP)
            first.setdefault("code", {})["rich_text"] = new_rt
            first["code"]["language"] = lang
            for b in run[1:]:
                delete_block(b["id"])
                time.sleep(RATE_LIMIT_SLEEP)
            skip.update(b["id"] for b in run[1:])
        except Exception as e:
            stats["errors"] += 1
            log(f"  ! failed to merge code blocks starting {first['id']}: {e}", "ERROR")
    return skip


# ---------------------------------------------------------------------------
# FIX 4: detect blocks containing only "##" / "#" / "###"
# ---------------------------------------------------------------------------

def is_only_heading_markers(block):
    if block.get("type") not in RICH_TEXT_BLOCK_TYPES:
        return False
    text = block_text(block)
    if not text.strip():
        return False
    return bool(ONLY_HEADING_MARKERS_RE.match(text))


# ---------------------------------------------------------------------------
# Notion update calls
# ---------------------------------------------------------------------------

def update_block_rich_text(block, new_rich_text):
    btype = block["type"]
    body = {btype: {"rich_text": new_rich_text}}
    if btype == "to_do":
        body["to_do"]["checked"] = block["to_do"].get("checked", False)
    if btype == "callout":
        icon = block["callout"].get("icon")
        if icon:
            body["callout"]["icon"] = icon
    notion_request("PATCH", f"/blocks/{block['id']}", body)


def update_table_row(row_block, new_cells):
    body = {"table_row": {"cells": new_cells}}
    notion_request("PATCH", f"/blocks/{row_block['id']}", body)


def update_code_block(block, new_rich_text, *, language=None):
    code_obj = {
        "rich_text": new_rich_text,
        "language": language or block["code"].get("language", "plain text"),
    }
    cap = block.get("code", {}).get("caption")
    if cap:
        code_obj["caption"] = cap
    notion_request("PATCH", f"/blocks/{block['id']}", {"code": code_obj})


def delete_block(block_id):
    notion_request("DELETE", f"/blocks/{block_id}")


# ---------------------------------------------------------------------------
# Walk + fix
# ---------------------------------------------------------------------------

def fix_blocks_under(parent_id, depth, max_depth, stats, dry_run, opts):
    if max_depth is not None and depth > max_depth:
        return

    children = get_block_children(parent_id)
    time.sleep(RATE_LIMIT_SLEEP)
    skip_ids = merge_consecutive_code_blocks(children, stats, dry_run, opts)

    for block in children:
        if block["id"] in skip_ids:
            continue
        btype = block.get("type")

        # FIX 4 (delete stray heading-marker blocks): check FIRST so we don't
        # waste API calls patching them
        if opts["do_strip_marker"] and is_only_heading_markers(block):
            stats["deleted_markers"] += 1
            preview = block_text(block).strip()
            log(f"  - {btype}: '{preview}' (heading-marker only, deleting)")
            if not dry_run:
                try:
                    delete_block(block["id"])
                    time.sleep(RATE_LIMIT_SLEEP)
                except Exception as e:
                    stats["errors"] += 1
                    log(f"  ! failed to delete block {block['id']}: {e}", "ERROR")
            continue  # don't recurse into a deleted block

        # FIX 1 + 2 on rich_text blocks
        if btype in RICH_TEXT_BLOCK_TYPES:
            payload = block.get(btype, {}) or {}
            rt = payload.get("rich_text", [])
            new_rt, counts, unbalanced = transform_rich_text(
                rt,
                do_underline=opts["do_underline"],
                do_strike_tilde=opts["do_strike_tilde"],
                do_mailto=opts["do_mailto"],
            )
            if unbalanced:
                stats["unbalanced"] += 1
                log(
                    f"  ! unbalanced <u> or ~~ in {btype} block {block['id']}, skipping",
                    "WARN",
                )
            total_fixes = (
                counts["underline"] + counts["strikethrough"] + counts["mailto"]
            )
            if total_fixes > 0:
                stats["fixed_underline"] += counts["underline"]
                stats["fixed_strikethrough"] += counts["strikethrough"]
                stats["fixed_mailto"] += counts["mailto"]
                preview = "".join((s.get("text") or {}).get("content", "") for s in new_rt)[
                    :80
                ]
                tag = []
                if counts["underline"]:
                    tag.append(f"u×{counts['underline']}")
                if counts["strikethrough"]:
                    tag.append(f"strike×{counts['strikethrough']}")
                if counts["mailto"]:
                    tag.append(f"mail×{counts['mailto']}")
                log(f"  ~ {btype} [{','.join(tag)}]: {preview!r}")
                if not dry_run:
                    try:
                        update_block_rich_text(block, new_rt)
                        time.sleep(RATE_LIMIT_SLEEP)
                    except Exception as e:
                        stats["errors"] += 1
                        log(f"  ! failed to update block {block['id']}: {e}", "ERROR")

        # FIX 3 on code blocks
        if btype == "code" and opts["do_code_blanks"]:
            new_rt, changed = transform_code_block(block)
            if changed:
                stats["fixed_code"] += 1
                log(f"  ~ code block {block['id']}: collapsed blank lines")
                if not dry_run:
                    try:
                        update_code_block(block, new_rt)
                        time.sleep(RATE_LIMIT_SLEEP)
                    except Exception as e:
                        stats["errors"] += 1
                        log(f"  ! failed to update code block {block['id']}: {e}", "ERROR")

        # Tables
        if btype == "table" and block.get("has_children"):
            rows = get_block_children(block["id"])
            time.sleep(RATE_LIMIT_SLEEP)
            for row in rows:
                if row.get("type") != "table_row":
                    continue
                cells = row["table_row"].get("cells", [])
                new_cells = []
                row_changed = False
                row_unbalanced = False
                row_counts = {"underline": 0, "strikethrough": 0, "mailto": 0}
                for cell in cells:
                    new_cell, counts, unbalanced = transform_rich_text(
                        cell,
                        do_underline=opts["do_underline"],
                        do_strike_tilde=opts["do_strike_tilde"],
                        do_mailto=opts["do_mailto"],
                    )
                    if unbalanced:
                        row_unbalanced = True
                    if counts["underline"] or counts["strikethrough"] or counts["mailto"]:
                        row_changed = True
                        row_counts["underline"] += counts["underline"]
                        row_counts["strikethrough"] += counts["strikethrough"]
                        row_counts["mailto"] += counts["mailto"]
                    new_cells.append(new_cell)
                if row_unbalanced:
                    stats["unbalanced"] += 1
                    log(
                        f"  ! unbalanced <u> or ~~ in table row {row['id']}, skipping",
                        "WARN",
                    )
                elif row_changed:
                    stats["fixed_underline"] += row_counts["underline"]
                    stats["fixed_strikethrough"] += row_counts["strikethrough"]
                    stats["fixed_mailto"] += row_counts["mailto"]
                    log(
                        f"  ~ table_row {row['id']}: u×{row_counts['underline']}, "
                        f"strike×{row_counts['strikethrough']}, mail×{row_counts['mailto']}"
                    )
                    if not dry_run:
                        try:
                            update_table_row(row, new_cells)
                            time.sleep(RATE_LIMIT_SLEEP)
                        except Exception as e:
                            stats["errors"] += 1
                            log(f"  ! failed to update row {row['id']}: {e}", "ERROR")
            continue  # skip recursion into table_rows

        # Recurse
        if block.get("has_children"):
            fix_blocks_under(block["id"], depth + 1, max_depth, stats, dry_run, opts)


def walk_pages_and_fix(page_id, depth, max_depth, stats, dry_run, opts, visited,
                        single_page=False):
    """Walk the page tree (or single page if single_page=True)."""
    if page_id in visited:
        return
    visited.add(page_id)

    page = notion_request("GET", f"/pages/{page_id}")
    title = ""
    props = page.get("properties", {})
    title_prop = props.get("title") or next((v for v in props.values() if v.get("type") == "title"), None)
    if title_prop and title_prop.get("title"):
        title = "".join(t.get("plain_text", "") for t in title_prop["title"])
    log(f"{'  ' * depth}* {title or '(untitled)'}  [{page_id}]")
    stats["pages_scanned"] += 1

    fix_blocks_under(page_id, depth, max_depth, stats, dry_run, opts)

    if single_page:
        return
    if max_depth is not None and depth >= max_depth:
        return
    for block in get_block_children(page_id):
        if block.get("type") == "child_page":
            walk_pages_and_fix(block["id"], depth + 1, max_depth, stats, dry_run, opts, visited)
    time.sleep(RATE_LIMIT_SLEEP)


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
        help="Notion page URL or ID. Walks the whole subtree below this page. "
             "Mutually exclusive with --url.",
    )
    parser.add_argument(
        "--url",
        help="Notion page URL or ID. Process this single page only (no recursion). "
             "Mutually exclusive with --parent-url.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Scan only, don't write")
    parser.add_argument("--max-depth", type=int, default=None, help="Max recursion depth (only with --parent-url)")
    parser.add_argument("--no-underline", action="store_true", help="Disable <u>...</u> -> underline fix")
    parser.add_argument(
        "--no-strike-tilde",
        action="store_true",
        help="Disable ~~...~~ -> strikethrough fix",
    )
    parser.add_argument("--no-mailto", action="store_true", help="Disable mailto link fix")
    parser.add_argument("--no-code-blanks", action="store_true", help="Disable code-block blank-line collapsing")
    parser.add_argument(
        "--no-merge-consecutive-code",
        action="store_true",
        help="Disable merging adjacent code blocks into one",
    )
    parser.add_argument("--no-strip-marker", action="store_true", help="Disable deletion of '##'-only blocks")
    args = parser.parse_args()

    if args.parent_url and args.url:
        log("--parent-url and --url are mutually exclusive", "ERROR")
        return 1

    target_url = args.url or args.parent_url or DEFAULT_PARENT_URL
    if not target_url:
        log("Missing target. Pass --parent-url, --url, or set NOTION_NOTES_PARENT_URL", "ERROR")
        return 1
    try:
        page_id = extract_notion_id(target_url)
    except ValueError as e:
        log(f"Invalid URL: {e}", "ERROR")
        return 1

    if not NOTION_TOKEN:
        log("NOTION_TOKEN env var is required", "ERROR")
        return 1

    single_page_mode = bool(args.url)
    log(f"Target page = {page_id}")
    log(f"Mode: {'SINGLE PAGE' if single_page_mode else 'TREE'} | "
        f"{'DRY RUN' if args.dry_run else 'LIVE'}")
    if args.max_depth is not None and not single_page_mode:
        log(f"Max depth = {args.max_depth}")

    enabled = []
    if not args.no_underline: enabled.append("<u>->underline")
    if not args.no_strike_tilde: enabled.append("~~->strikethrough")
    if not args.no_mailto: enabled.append("mailto-links")
    if not args.no_code_blanks: enabled.append("code-blanks")
    if not args.no_merge_consecutive_code: enabled.append("merge-consecutive-code")
    if not args.no_strip_marker: enabled.append("strip-##-blocks")
    log(f"Fixes enabled: {', '.join(enabled) if enabled else '(none)'}")
    if not enabled:
        log("All fixes disabled - nothing to do", "WARN")
        return 0

    opts = {
        "do_underline": not args.no_underline,
        "do_strike_tilde": not args.no_strike_tilde,
        "do_mailto": not args.no_mailto,
        "do_code_blanks": not args.no_code_blanks,
        "do_merge_code_blocks": not args.no_merge_consecutive_code,
        "do_strip_marker": not args.no_strip_marker,
    }
    stats = {
        "pages_scanned": 0,
        "fixed_underline": 0,
        "fixed_strikethrough": 0,
        "fixed_mailto": 0,
        "fixed_code": 0,
        "merged_code_blocks": 0,
        "deleted_markers": 0,
        "unbalanced": 0,
        "errors": 0,
    }
    visited = set()

    try:
        walk_pages_and_fix(
            page_id, 0,
            args.max_depth if not single_page_mode else 0,
            stats, args.dry_run, opts, visited,
            single_page=single_page_mode,
        )
    except KeyboardInterrupt:
        log("Interrupted by user", "WARN")

    log("")
    log("=== SUMMARY ===")
    log(f"Pages scanned:           {stats['pages_scanned']}")
    log(f"Underline fixes:         {stats['fixed_underline']}")
    log(f"Strikethrough (~~) fixes: {stats['fixed_strikethrough']}")
    log(f"Mailto link fixes:       {stats['fixed_mailto']}")
    log(f"Code-block fixes:        {stats['fixed_code']}")
    log(f"Code blocks merged in:   {stats['merged_code_blocks']}")
    log(f"Heading-marker deletes:  {stats['deleted_markers']}")
    log(f"Unbalanced <u> / ~~ :     {stats['unbalanced']}  (left untouched)")
    log(f"API errors:              {stats['errors']}")
    if args.dry_run:
        log("(dry run - nothing was actually written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
