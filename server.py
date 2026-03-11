#!/usr/bin/env python3
"""
apple_notes_mcp.py

MCP server that exposes Apple Notes to Claude.

Requires:
    mcp       (pip install mcp)
    html2text (pip install html2text)

Platform: macOS only (uses osascript / JXA)

Usage:
    python3 server.py

Claude Desktop config (~/.../claude_desktop_config.json):
    {
      "mcpServers": {
        "apple-notes": {
          "command": "/path/to/python3",
          "args": ["/path/to/apple-notes-mcp/server.py"]
        }
      }
    }
"""

import concurrent.futures
import json
import subprocess
import time
from pathlib import Path

import html2text as _html2text
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Apple Notes")

CACHE_PATH = Path.home() / ".cache" / "apple-notes-mcp" / "notes.json"
BODY_BATCH_SIZE = 50
BODY_WORKERS = 8

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript error")
    return result.stdout.strip()


def _run_jxa(script: str, timeout: int = 30) -> str:
    """Run a JavaScript for Automation (JXA) script via osascript."""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "JXA error")
    return result.stdout.strip()


def _html_to_markdown(html: str) -> str:
    converter = _html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = False
    converter.body_width = 0
    converter.protect_links = True
    converter.unicode_snob = True
    return converter.handle(html).strip()


def _fetch_all_notes() -> list[tuple[str, str]]:
    """Return [(title, folder), ...] for all notes using bulk JXA property access."""
    script = """
const app = Application("Notes");
const names = app.notes.name();
const folders = app.notes.container.name();
const result = [];
for (var i = 0; i < names.length; i++) {
    result.push({t: names[i] || "", f: folders[i] || ""});
}
JSON.stringify(result);
"""
    data = json.loads(_run_jxa(script, timeout=60))
    return [(d["t"], d["f"] or "") for d in data]


def _fetch_note_index() -> list[dict]:
    """Return [{title, folder, created_ms, modified_ms}, ...] via bulk JXA property access."""
    script = """
const app = Application("Notes");
const names = app.notes.name();
const folders = app.notes.container.name();
const created = app.notes.creationDate();
const modified = app.notes.modificationDate();
const result = [];
for (var i = 0; i < names.length; i++) {
    result.push({
        title: names[i] || "",
        folder: folders[i] || "",
        created_ms: created[i] ? created[i].getTime() : 0,
        modified_ms: modified[i] ? modified[i].getTime() : 0
    });
}
JSON.stringify(result);
"""
    return json.loads(_run_jxa(script, timeout=60))


def _fetch_bodies_batch(start: int, count: int) -> list[tuple[str, str]]:
    """Fetch [(title, body_html), ...] for notes[start:start+count] via JXA."""
    script = f"""
const app = Application("Notes");
const result = [];
const end = Math.min({start} + {count}, app.notes.length);
for (var i = {start}; i < end; i++) {{
    try {{
        result.push({{name: app.notes[i].name(), body: app.notes[i].body()}});
    }} catch(e) {{
        result.push({{name: "", body: ""}});
    }}
}}
JSON.stringify(result);
"""
    data = json.loads(_run_jxa(script, timeout=120))
    return [(d["name"], d["body"]) for d in data if d["name"]]


def _load_cache() -> dict[str, dict]:
    """Load cache from disk. Returns {title: {folder, modified_ms, body_md}}."""
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))


def _get_body_cache(report_progress: bool = False) -> tuple[dict[str, dict], bool]:
    """
    Return an up-to-date body cache: {title: {folder, modified_ms, body_md}}.
    Fetches only notes that are new or modified since the last cache build.
    Returns (cache, was_rebuilt) where was_rebuilt=True if any bodies were fetched.
    """
    index = _fetch_note_index()  # fast: ~0.5s
    cache = _load_cache()

    stale = [n for n in index if
             n["title"] not in cache or
             cache[n["title"]]["modified_ms"] != n["modified_ms"]]

    if not stale:
        return cache, False

    if report_progress:
        print(f"[apple-notes-mcp] Fetching {len(stale)} note bodies "
              f"({'full build' if len(stale) == len(index) else 'incremental update'})...",
              flush=True)

    # Map title -> index position for batch fetching
    title_to_pos = {n["title"]: i for i, n in enumerate(index)}
    stale_positions = sorted(title_to_pos[n["title"]] for n in stale
                             if n["title"] in title_to_pos)

    # Build batches of contiguous positions
    batches: list[tuple[int, int]] = []
    if stale_positions:
        start = stale_positions[0]
        prev = start
        for pos in stale_positions[1:]:
            if pos - prev > BODY_BATCH_SIZE:
                batches.append((start, prev - start + 1))
                start = pos
            prev = pos
        batches.append((start, prev - start + 1))

    # Parallel fetch
    fetched: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=BODY_WORKERS) as ex:
        futures = {ex.submit(_fetch_bodies_batch, s, c): (s, c) for s, c in batches}
        for fut in concurrent.futures.as_completed(futures):
            try:
                for title, body_html in fut.result():
                    fetched[title] = _html_to_markdown(body_html)
            except Exception:
                pass

    # Update cache entries
    for n in index:
        title = n["title"]
        cache[title] = {
            "folder": n["folder"],
            "modified_ms": n["modified_ms"],
            "body_md": fetched.get(title, cache.get(title, {}).get("body_md", "")),
        }

    # Remove deleted notes
    live_titles = {n["title"] for n in index}
    for title in list(cache.keys()):
        if title not in live_titles:
            del cache[title]

    _save_cache(cache)
    return cache, True


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_folders() -> str:
    """List all folders in Apple Notes."""
    script = """
const app = Application("Notes");
JSON.stringify(app.folders.name());
"""
    folders = json.loads(_run_jxa(script, timeout=60))
    if not folders:
        return "No folders found."
    return "\n".join(f"- {f}" for f in folders)


@mcp.tool()
def list_notes(folder: str = "") -> str:
    """
    List Apple Notes with metadata (folder, created, modified).

    Args:
        folder: Optional folder name to filter by. Leave empty to list all notes.
    """
    from datetime import datetime, timezone

    def fmt_ms(ms: int) -> str:
        if not ms:
            return "unknown"
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    notes = _fetch_note_index()

    if folder:
        notes = [n for n in notes if n["folder"].lower() == folder.lower()]

    if not notes:
        folder_suffix = f' in folder "{folder}"' if folder else ''
        return f"No notes found{folder_suffix}."

    lines_out = []
    current_folder = None
    for n in sorted(notes, key=lambda x: (x["folder"].lower(), x["title"].lower())):
        if n["folder"] != current_folder:
            if current_folder is not None:
                lines_out.append("")
            lines_out.append(f"**{n['folder'] or '(no folder)'}**")
            current_folder = n["folder"]
        created = fmt_ms(n["created_ms"])
        modified = fmt_ms(n["modified_ms"])
        lines_out.append(f"  - {n['title']}  [created: {created}, modified: {modified}]")

    return "\n".join(lines_out)


@mcp.tool()
def get_note(title: str) -> str:
    """
    Get the full content of an Apple Note by its title.

    Args:
        title: The exact title of the note to retrieve.
    """
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    script = f"""
tell application "Notes"
    set matchingNotes to (notes whose name is "{safe_title}")
    if (count of matchingNotes) is 0 then
        return "ERROR: No note found with title: {safe_title}"
    end if
    return body of item 1 of matchingNotes
end tell
"""
    result = _run_applescript(script)
    if result.startswith("ERROR:"):
        return result
    return _html_to_markdown(result)


@mcp.tool()
def search_notes(query: str, search_body: bool = True) -> str:
    """
    Search Apple Notes by title, or optionally by full content.

    Title search is instant (~0.5s). Body search uses a persistent disk cache —
    the first call builds the cache (~60-90s for large libraries); every
    subsequent call is fast and only re-fetches notes that have changed.

    Args:
        query: Text to search for (case-insensitive).
        search_body: If True, search note bodies in addition to titles.
                     Default is False (title-only, instant).
    """
    if not query.strip():
        return "Please provide a non-empty search query."

    q_lower = query.lower()

    if not search_body:
        notes = _fetch_all_notes()
        matches = [(t, f, "title") for t, f in notes if q_lower in t.lower()]
        suffix = ""
    else:
        t0 = time.time()
        cache, rebuilt = _get_body_cache(report_progress=True)
        elapsed = time.time() - t0

        matches = []
        for title, entry in cache.items():
            folder = entry.get("folder", "")
            body_md = entry.get("body_md", "")
            in_title = q_lower in title.lower()
            in_body = q_lower in body_md.lower()
            if in_title or in_body:
                match_type = "title+body" if (in_title and in_body) else ("title" if in_title else "body")
                matches.append((title, folder, match_type))

        suffix = f"\n\n_({'Cache rebuilt' if rebuilt else 'Cache hit'} in {elapsed:.1f}s)_"

    if not matches:
        return f'No notes found matching "{query}".{suffix if search_body else ""}'

    lines = [f'Found {len(matches)} note(s) matching "{query}":\n']
    for title, folder, match_type in sorted(matches, key=lambda x: (x[1].lower(), x[0].lower())):
        lines.append(f"  - **{title}** ({folder}) — {match_type}")
    return "\n".join(lines) + (suffix if search_body else "")


@mcp.tool()
def rebuild_search_cache() -> str:
    """
    Force a full rebuild of the body search cache.

    Use this if notes seem out of date in search results.
    This may take 60-90 seconds for large note libraries.
    """
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()
    t0 = time.time()
    cache, _ = _get_body_cache(report_progress=True)
    elapsed = time.time() - t0
    return f"Cache rebuilt: {len(cache)} notes indexed in {elapsed:.0f}s."


if __name__ == "__main__":
    mcp.run()
