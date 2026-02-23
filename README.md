# apple-notes-mcp

An [MCP](https://modelcontextprotocol.io/) server that exposes Apple Notes to Claude (or any MCP-compatible client).

**macOS only** — uses AppleScript / JXA via `osascript`.

## Features

- List all folders and notes with metadata (created, modified dates)
- Get the full content of any note by title (returned as Markdown)
- Search notes by title or full body text
- Persistent disk cache for fast body search — only re-fetches notes that have changed

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
