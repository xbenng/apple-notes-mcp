# apple-notes-mcp

An [MCP](https://modelcontextprotocol.io/) server that exposes Apple Notes to Claude (or any MCP-compatible client).

**macOS only** — uses AppleScript / JXA via `osascript`.

## Features

- List folders (grouped by account, with note counts) and notes with metadata, most recently modified first
- Get the full content of any note as Markdown, by id or exact title (ambiguous titles list the candidate ids)
- Search notes by title or full body text, with match snippets
- Create notes, and edit existing ones (replace or append HTML body; no delete)
- Persistent disk cache for fast body search: only re-fetches notes that have changed
- Skips "Recently Deleted"; password-protected notes are listed but not read
- Inline base64 images are stripped from returned and searched content

Notes are identified by their Notes id (`x-coredata://...`) because titles are not unique.

## Requirements

- macOS
- Python 3.10+
- [mcp](https://pypi.org/project/mcp/) (`pip install mcp`)
- [html2text](https://pypi.org/project/html2text/) (`pip install html2text`)

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/apple-notes-mcp.git
cd apple-notes-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

In the config examples below, replace `/path/to/apple-notes-mcp` with the absolute path to the cloned repo.

### Claude Code (CLI)

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "apple-notes-mcp": {
      "command": "/path/to/apple-notes-mcp/.venv/bin/python",
      "args": ["/path/to/apple-notes-mcp/server.py"]
    }
  }
}
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apple-notes": {
      "command": "/path/to/apple-notes-mcp/.venv/bin/python",
      "args": ["/path/to/apple-notes-mcp/server.py"]
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `list_folders` | List all folders in Apple Notes |
| `list_notes` | List notes with metadata, optionally filtered by folder |
| `get_note` | Get full content of a note by title |
| `search_notes` | Search by title and/or body text |
| `rebuild_search_cache` | Force a full rebuild of the body search cache |

## Notes

- Body search builds a cache at `~/.cache/apple-notes-mcp/notes.json` on first use (~60–90s for large libraries). Subsequent searches are fast and incremental.
- Apple Notes must be authorized for automation access in **System Settings → Privacy & Security → Automation**.

### Remote (Claude Code on another host)

The server speaks stdio, so a Linux host can run it over SSH (Notes automation works from an SSH session):

```bash
claude mcp add -s user apple-notes -- ssh -o BatchMode=yes -o ConnectTimeout=10 \
  mac.lan /path/to/apple-notes-mcp/.venv/bin/python /path/to/apple-notes-mcp/server.py
```
