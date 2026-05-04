#!/usr/bin/env python3
"""Quick offline test of the markdown cleanup logic.

Run this on a single .md file to preview how it'll look after cleanup,
without needing Notion or Drive API.

Usage:
    python3 test_cleanup.py <path-to-some.md>
"""
import sys
from pathlib import Path

# Reuse the cleanup functions from the main script
sys.path.insert(0, str(Path(__file__).parent))
from migrate_notes_to_notion import (
    clean_icloud_md,
    replace_attachments,
    md_to_notion_blocks,
    title_from_filename,
    should_skip_note,
)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-some.md>")
        return 1
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Not found: {path}")
        return 1

    raw = path.read_text(encoding="utf-8", errors="replace")
    title = title_from_filename(path.name)
    skip, reason = should_skip_note(title, raw)

    print("=" * 60)
    print(f"FILE:   {path.name}")
    print(f"TITLE:  {title}")
    print(f"SIZE:   {len(raw)} bytes raw")
    print(f"SKIP:   {skip} ({reason or 'no'})")
    print("=" * 60)

    cleaned = clean_icloud_md(raw)
    cleaned = replace_attachments(cleaned, attachment_map={})
    print("\n--- CLEANED MARKDOWN ---\n")
    print(cleaned)

    blocks = md_to_notion_blocks(cleaned)
    print("\n" + "=" * 60)
    print(f"NOTION BLOCKS: {len(blocks)}")
    print("=" * 60)
    for i, b in enumerate(blocks[:20]):
        t = b["type"]
        text_field = b[t].get("rich_text", [])
        text = text_field[0]["text"]["content"] if text_field else ""
        print(f"  [{i:02d}] {t}: {text[:80]}")
    if len(blocks) > 20:
        print(f"  ... and {len(blocks) - 20} more blocks")

    return 0


if __name__ == "__main__":
    sys.exit(main())
