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

Notes are identified by their Notes id (x-coredata://...), since titles are
not unique. Tools that take a note accept either an id or an exact title;
an ambiguous title returns the candidates instead of guessing.

Claude Desktop config (~/.../claude_desktop_config.json):
    {
      "mcpServers": {
        "apple-notes": {
          "command": "/path/to/apple-notes-mcp/.venv/bin/python",
          "args": ["/path/to/apple-notes-mcp/server.py"]
        }
      }
    }

Remote (e.g. Claude Code on a Linux host), as a streamable-HTTP server:
    python3 server.py --http [--host 0.0.0.0] [--port 3002]
Clients must send "Authorization: Bearer <token>", where the token comes from
$APPLE_NOTES_MCP_TOKEN or ~/.config/apple-notes-mcp/token (created on first run).
The MCP endpoint is http://<mac>:3002/mcp.
"""

import argparse
import concurrent.futures
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path

import html2text as _html2text
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Apple Notes")

CACHE_PATH = Path.home() / ".cache" / "apple-notes-mcp" / "notes-v2.json"
BODY_BATCH_SIZE = 50
BODY_WORKERS = 8
BULK_FOLDER_MIN = 20  # stale notes in a folder before fetching the whole folder at once
DELETED_FOLDER = "Recently Deleted"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_jxa(script: str, *args: str, timeout: int = 60) -> str:
    """
    Run a JXA script via osascript. The script must define run(argv); args are
    passed as argv so user input is never spliced into script source.
    """
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-", *args],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "JXA error")
    return result.stdout.strip()


_DATA_URI = re.compile(r"""(src=["'])data:[^"']*""")
# Notes returns entities without the trailing semicolon ("&lt" for "<"), which
# html2text misreads when a letter follows. A literal "&" always comes back as
# "&amp", so every "&" starts an entity and restoring the ";" is safe.
_BARE_ENTITY = re.compile(r"&(lt|gt|amp|quot|apos|nbsp)(?!;)")


def _html_to_markdown(body: str) -> str:
    # Inline images are base64 data URIs; drop the payload so it isn't
    # returned or searched.
    body = _DATA_URI.sub(r"\1data:omitted", body)
    body = _BARE_ENTITY.sub(r"&\1;", body)
    converter = _html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = False
    converter.body_width = 0
    converter.protect_links = True
    converter.unicode_snob = True
    return converter.handle(body).strip()


def _text_to_html(text: str) -> str:
    """Plain text -> Notes HTML, one <div> per line."""
    return "".join(
        f"<div>{html.escape(line) if line else '<br>'}</div>"
        for line in text.split("\n")
    )


def _fmt_ms(ms: int) -> str:
    if not ms:
        return "unknown"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _fetch_note_index(include_deleted: bool = False) -> list[dict]:
    """
    Return [{id, title, folder, folder_id, pos, account, created_ms, modified_ms, locked}, ...]
    (pos is the note's index within its folder)
    via bulk JXA property fetches (one round of calls per folder).
    """
    script = """
function run(argv) {
    const app = Application("Notes");
    const out = [];
    for (const acct of app.accounts()) {
        const acctName = acct.name();
        for (const folder of acct.folders()) {
            const notes = folder.notes;
            const ids = notes.id();
            if (!ids.length) continue;
            const folderName = folder.name();
            const folderId = folder.id();
            const names = notes.name();
            const created = notes.creationDate();
            const modified = notes.modificationDate();
            const locked = notes.passwordProtected();
            for (let i = 0; i < ids.length; i++) {
                out.push({
                    id: ids[i],
                    title: names[i] || "",
                    folder: folderName,
                    folder_id: folderId,
                    pos: i,
                    account: acctName,
                    created_ms: created[i] ? created[i].getTime() : 0,
                    modified_ms: modified[i] ? modified[i].getTime() : 0,
                    locked: !!locked[i]
                });
            }
        }
    }
    return JSON.stringify(out);
}
"""
    notes = json.loads(_run_jxa(script))
    if not include_deleted:
        notes = [n for n in notes if n["folder"] != DELETED_FOLDER]
    return notes


def _fetch_bodies(ids: list[str]) -> dict[str, str]:
    """Fetch {id: body_html} for the given note ids, one lookup each."""
    script = """
function run(argv) {
    const app = Application("Notes");
    const out = {};
    for (const id of argv) {
        try { out[id] = app.notes.byId(id).body(); } catch (e) {}
    }
    return JSON.stringify(out);
}
"""
    return json.loads(_run_jxa(script, *ids, timeout=180))


def _fetch_folder_bodies(folder_id: str) -> dict[str, str]:
    """Fetch {id: body_html} for every note in a folder with one bulk call."""
    script = """
function run(argv) {
    const notes = Application("Notes").folders.byId(argv[0]).notes;
    const ids = notes.id();
    const bodies = notes.body();
    const out = {};
    for (let i = 0; i < ids.length; i++) out[ids[i]] = bodies[i];
    return JSON.stringify(out);
}
"""
    return json.loads(_run_jxa(script, folder_id, timeout=120))


def _fetch_folder_range(folder_id: str, start: int, end: int) -> dict[str, str]:
    """Fetch {id: body_html} for notes[start:end] of a folder, one note at a time."""
    script = """
function run(argv) {
    const notes = Application("Notes").folders.byId(argv[0]).notes;
    const ids = notes.id();
    const end = Math.min(Number(argv[2]), ids.length);
    const out = {};
    for (let i = Number(argv[1]); i < end; i++) {
        try { out[ids[i]] = notes[i].body(); } catch (e) {}
    }
    return JSON.stringify(out);
}
"""
    return json.loads(_run_jxa(script, folder_id, str(start), str(end), timeout=180))


def _fetch_stale_bodies(stale: list[dict]) -> dict[str, str]:
    """
    Fetch {id: body_html} for the stale index entries. Folders with many stale
    notes are fetched in bulk (falling back to parallel positional chunks when
    the bulk call fails, which Notes does for some large folders); stragglers
    are fetched by id.
    """
    by_folder: dict[str, list[dict]] = {}
    for n in stale:
        by_folder.setdefault(n["folder_id"], []).append(n)

    out: dict[str, str] = {}
    by_id: list[str] = []

    def fetch_folder(fid: str, notes: list[dict]) -> dict[str, str]:
        try:
            return _fetch_folder_bodies(fid)
        except Exception:
            pass
        lo = min(n["pos"] for n in notes)
        hi = max(n["pos"] for n in notes) + 1
        result: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=BODY_WORKERS) as ex:
            futs = [ex.submit(_fetch_folder_range, fid, i, min(i + BODY_BATCH_SIZE, hi))
                    for i in range(lo, hi, BODY_BATCH_SIZE)]
            for fut in concurrent.futures.as_completed(futs):
                try:
                    result.update(fut.result())
                except Exception:
                    pass
        return result

    for fid, notes in by_folder.items():
        if len(notes) >= BULK_FOLDER_MIN:
            out.update(fetch_folder(fid, notes))
        else:
            by_id.extend(n["id"] for n in notes)

    wanted = {n["id"] for n in stale}
    # Notes may have moved since the index was taken; pick up any misses by id.
    by_id.extend(i for i in wanted if i not in out and i not in by_id)
    batches = [by_id[i:i + BODY_BATCH_SIZE] for i in range(0, len(by_id), BODY_BATCH_SIZE)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=BODY_WORKERS) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(_fetch_bodies, b) for b in batches]):
            try:
                out.update(fut.result())
            except Exception:
                pass
    return {i: b for i, b in out.items() if i in wanted}


def _load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(CACHE_PATH)


def _get_body_cache() -> tuple[dict[str, dict], int]:
    """
    Return an up-to-date cache {id: {title, folder, account, modified_ms, body_md}}
    and the number of bodies (re)fetched. Only new or modified notes are fetched.
    """
    index = _fetch_note_index()
    cache = _load_cache()

    stale = [n for n in index
             if not n["locked"] and (
                 n["id"] not in cache or
                 cache[n["id"]]["modified_ms"] != n["modified_ms"])]

    fetched = {nid: _html_to_markdown(body)
               for nid, body in _fetch_stale_bodies(stale).items()} if stale else {}

    new_cache = {}
    for n in index:
        prev = cache.get(n["id"], {})
        if n["id"] in fetched:
            body_md, mod = fetched[n["id"]], n["modified_ms"]
        else:
            # Keep the old body (and old modified_ms, so a failed fetch retries).
            body_md, mod = prev.get("body_md", ""), prev.get("modified_ms", 0)
        new_cache[n["id"]] = {
            "title": n["title"],
            "folder": n["folder"],
            "account": n["account"],
            "modified_ms": mod,
            "body_md": body_md,
        }

    if stale or len(new_cache) != len(cache):
        _save_cache(new_cache)
    return new_cache, len(fetched)


def _resolve_note(note: str, folder: str = "") -> tuple[dict | None, str]:
    """
    Resolve an id or exact title to a single index entry.
    Returns (entry, "") or (None, message explaining why).
    """
    index = _fetch_note_index(include_deleted=note.startswith("x-coredata://"))
    if note.startswith("x-coredata://"):
        hits = [n for n in index if n["id"] == note]
    else:
        hits = [n for n in index if n["title"] == note]
        if not hits:
            hits = [n for n in index if n["title"].lower() == note.lower()]
    if folder:
        hits = [n for n in hits if n["folder"].lower() == folder.lower()]

    if not hits:
        return None, f'No note found matching "{note}"' + (f' in folder "{folder}"' if folder else "") + "."
    if len(hits) > 1:
        lines = [f'{len(hits)} notes are titled "{note}"; pass one of these ids:']
        for n in sorted(hits, key=lambda x: -x["modified_ms"]):
            lines.append(f"  - {n['id']}  ({n['account']} / {n['folder']}, "
                         f"modified {_fmt_ms(n['modified_ms'])})")
        return None, "\n".join(lines)
    return hits[0], ""


def _snippet(text: str, q_lower: str, width: int = 80) -> str:
    pos = text.lower().find(q_lower)
    if pos < 0:
        return ""
    start = max(0, pos - width)
    end = min(len(text), pos + len(q_lower) + width)
    s = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_folders() -> str:
    """List all Apple Notes folders, grouped by account, with note counts."""
    script = """
function run(argv) {
    const app = Application("Notes");
    const out = [];
    for (const acct of app.accounts()) {
        const names = acct.folders.name();
        const counts = acct.folders().map(f => f.notes.length);
        out.push({account: acct.name(), folders: names.map((n, i) => [n, counts[i]])});
    }
    return JSON.stringify(out);
}
"""
    data = json.loads(_run_jxa(script))
    lines = []
    for acct in data:
        lines.append(f"**{acct['account']}**")
        for name, count in acct["folders"]:
            lines.append(f"  - {name} ({count})")
    return "\n".join(lines) or "No folders found."


@mcp.tool()
def list_notes(folder: str = "", account: str = "", limit: int = 200) -> str:
    """
    List Apple Notes with metadata, most recently modified first.

    Args:
        folder: Optional folder name to filter by (case-insensitive).
        account: Optional account name to filter by (e.g. "iCloud").
        limit: Maximum notes to return (default 200).
    """
    notes = _fetch_note_index(include_deleted=folder.lower() == DELETED_FOLDER.lower())
    if folder:
        notes = [n for n in notes if n["folder"].lower() == folder.lower()]
    if account:
        notes = [n for n in notes if n["account"].lower() == account.lower()]
    if not notes:
        return "No notes found."

    notes.sort(key=lambda n: -n["modified_ms"])
    lines = [f"{len(notes)} note(s)" + (f", showing {limit}" if len(notes) > limit else "") + ":"]
    for n in notes[:limit]:
        lock = " 🔒" if n["locked"] else ""
        lines.append(f"  - {n['title'] or '(untitled)'}{lock}  [{n['account']} / {n['folder']}, "
                     f"modified {_fmt_ms(n['modified_ms'])}]  id={n['id']}")
    return "\n".join(lines)


@mcp.tool()
def get_note(note: str, folder: str = "") -> str:
    """
    Get the full content of an Apple Note as Markdown.

    Args:
        note: The note's id (x-coredata://...) or its exact title.
        folder: Optional folder name to disambiguate a duplicated title.
    """
    entry, err = _resolve_note(note, folder)
    if not entry:
        return err
    if entry["locked"]:
        return f'"{entry["title"]}" is password-protected; its content is not readable.'
    body = _fetch_bodies([entry["id"]]).get(entry["id"])
    if body is None:
        return f"Could not read note {entry['id']}."
    header = (f"# {entry['title']}\n_{entry['account']} / {entry['folder']} · "
              f"created {_fmt_ms(entry['created_ms'])} · modified {_fmt_ms(entry['modified_ms'])} · "
              f"id {entry['id']}_\n\n")
    return header + _html_to_markdown(body)


@mcp.tool()
def search_notes(query: str, search_body: bool = True, limit: int = 50) -> str:
    """
    Search Apple Notes by title and (by default) full body text, case-insensitive.

    Title-only search takes a few seconds. Body search uses a persistent disk
    cache: the first call builds it (a minute or two for large libraries);
    later calls only re-fetch notes that changed.

    Args:
        query: Text to search for.
        search_body: Also search note bodies (default True).
        limit: Maximum results to return (default 50).
    """
    if not query.strip():
        return "Please provide a non-empty search query."
    q = query.lower()

    suffix = ""
    if not search_body:
        matches = [(n, "title", "") for n in _fetch_note_index() if q in n["title"].lower()]
    else:
        t0 = time.time()
        cache, fetched = _get_body_cache()
        matches = []
        for nid, e in cache.items():
            in_title = q in e["title"].lower()
            in_body = q in e["body_md"].lower()
            if in_title or in_body:
                kind = "title+body" if in_title and in_body else ("title" if in_title else "body")
                matches.append(({**e, "id": nid}, kind, _snippet(e["body_md"], q) if in_body else ""))
        suffix = f"\n\n_(searched {len(cache)} notes in {time.time() - t0:.1f}s; {fetched} bodies refreshed)_"

    if not matches:
        return f'No notes found matching "{query}".{suffix}'

    matches.sort(key=lambda m: -m[0]["modified_ms"])
    lines = [f'Found {len(matches)} note(s) matching "{query}"'
             + (f", showing {limit}" if len(matches) > limit else "") + ":\n"]
    for n, kind, snip in matches[:limit]:
        lines.append(f"  - **{n['title'] or '(untitled)'}** ({n['account']} / {n['folder']}) — {kind}  id={n['id']}")
        if snip:
            lines.append(f"    > {snip}")
    return "\n".join(lines) + suffix


@mcp.tool()
def rebuild_search_cache() -> str:
    """Force a full rebuild of the body search cache (a minute or two)."""
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()
    t0 = time.time()
    cache, fetched = _get_body_cache()
    return f"Cache rebuilt: {len(cache)} notes indexed ({fetched} bodies) in {time.time() - t0:.0f}s."


@mcp.tool()
def create_note(title: str, body: str = "", folder: str = "Notes", account: str = "iCloud") -> str:
    """
    Create a new Apple Note.

    Args:
        title: Note title (becomes the first line).
        body: Plain-text body; newlines become separate lines.
        folder: Folder to create it in (default "Notes").
        account: Account holding the folder (default "iCloud").
    """
    script = """
function run(argv) {
    const [acctName, folderName, html] = argv;
    const app = Application("Notes");
    const acct = app.accounts.byName(acctName);
    const matches = acct.folders.whose({name: folderName})();
    if (!matches.length) return "ERROR: no folder " + JSON.stringify(folderName) + " in account " + JSON.stringify(acctName);
    const note = app.Note({body: html});
    matches[0].notes.push(note);
    return note.id();
}
"""
    html_body = f"<div><h1>{html.escape(title)}</h1></div>" + (_text_to_html(body) if body else "")
    result = _run_jxa(script, account, folder, html_body)
    if result.startswith("ERROR:"):
        return result
    return f'Created "{title}" in {account} / {folder}. id={result}'


@mcp.tool()
def edit_note(note: str, content: str, append: bool = False, folder: str = "") -> str:
    """
    Edit an Apple Note's body content.

    Args:
        note: The note's id (x-coredata://...) or its exact title.
        content: New content in HTML format. Supports standard HTML tags
                 (div, p, br, b, i, u, ul, ol, li, a, h1-h6, blockquote, pre, code).
                 Plain text without tags is also accepted.
        append: If True, append content to the end of the existing note.
                If False (default), replace the entire note body, including the
                title line (Notes takes the title from the body's first line).
        folder: Optional folder name to disambiguate a duplicated title.
    """
    entry, err = _resolve_note(note, folder)
    if not entry:
        return err
    if entry["locked"]:
        return f'"{entry["title"]}" is password-protected; cannot modify it.'
    script = """
function run(argv) {
    const [id, html, append] = argv;
    const n = Application("Notes").notes.byId(id);
    n.body = append === "1" ? n.body() + html : html;
    return "ok";
}
"""
    _run_jxa(script, entry["id"], content, "1" if append else "0")
    return (f'Note "{entry["title"]}" {"appended to" if append else "updated"} '
            f'({entry["account"]} / {entry["folder"]}). id={entry["id"]}')


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

TOKEN_PATH = Path.home() / ".config" / "apple-notes-mcp" / "token"


def _load_token() -> str:
    """Bearer token from $APPLE_NOTES_MCP_TOKEN, else the token file (created if missing)."""
    token = os.environ.get("APPLE_NOTES_MCP_TOKEN", "").strip()
    if token:
        return token
    if not TOKEN_PATH.exists():
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    return TOKEN_PATH.read_text().strip()


class _BearerAuth:
    """ASGI middleware: reject HTTP requests without the expected bearer token."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized\n"})
                return
        await self.app(scope, receive, send)


def _run_http(host: str, port: int) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    # Every request carries the bearer token, which a DNS-rebinding page can't
    # supply, so the Host-header allowlist (localhost-only by default) is off.
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp.settings.stateless_http = True  # no session state to lose across restarts
    app = _BearerAuth(mcp.streamable_http_app(), _load_token())
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apple Notes MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3002)
    args = parser.parse_args()
    if args.http:
        _run_http(args.host, args.port)
    else:
        mcp.run()
