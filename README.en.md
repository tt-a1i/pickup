<div align="center">

<img src="assets/cover.jpg" alt="pickup — Find & read any local AI agent session" width="100%" />

# pickup

**Let any AI coding agent find and read another local session in seconds.**

*Cross-project, cross-tool local session discovery and progressive reading — zero format conversion, zero background daemons, zero third-party dependencies.*

<p>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10%2B-2563EB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/Dependencies-Zero%20(stdlib)-10B981?style=flat-square" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/Agents-7%20Supported-0EA5E9?style=flat-square" alt="7 Agents Supported">
  <img src="https://img.shields.io/badge/Mode-Local%20Read--Only-F59E0B?style=flat-square" alt="Local Read-Only">
  <img src="https://img.shields.io/badge/Tests-25%20Passing-16A34A?style=flat-square" alt="25 Tests Passing">
  <a href="https://github.com/tt-a1i/pickup/commits/main"><img src="https://img.shields.io/github/last-commit/tt-a1i/pickup?style=flat-square&color=64748B" alt="Last Commit"></a>
</p>

<p>
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

</div>

---

## Why `pickup`?

You explored an architecture in **Antigravity** and want **Codex** to implement it — or a session in **Kimi Code** / **Qoder** stalled mid-task and you want to switch tools without losing context. All of that history is already sitting on your local disk; the new agent just needs a reliable way to locate and read it.

| Common pain point | How `pickup` solves it |
| :--- | :--- |
| Scattered session files & SQLite DBs tied to specific directories | **Global cross-tool discovery**: searches across 7 local agents regardless of `cwd`, by verbatim quote, title, or topic |
| Feeding a 3,000-step session from the top chokes or derails the model | **Progressive disclosure**: gives you the full user-prompt outline + tail turns first, then reads exact ranges or greps on demand |
| Heavy indexers, 2 GB duplicate DBs, or always-on MCP context bloat | **Zero daemon, zero index, read-only**: Python standard library only; never modifies source stores or sends network requests |
| Handoff tools forcing unsolicited next actions | **Strictly non-intrusive**: loads the history cleanly and lets your current prompt decide whether to summarize, rethink, or continue coding |

---

## Supported Agents

| Agent | Default Local Store | What `pickup` Reads |
| :--- | :--- | :--- |
| **Antigravity** | `~/.gemini/antigravity/brain/` | Full main-session transcript + recent background task logs |
| **Codex** | `~/.codex/sessions/` | Main-session JSONL (filters injected system prompt blocks) |
| **Claude Code** | `~/.claude/projects/` | Project main-session JSONL (supports array-of-text-blocks tool results) |
| **Qoder** | `~/.qoder/projects/`, `~/.qoder-cn/projects/`, `~/.qoderwork/projects/` | Project session JSONL (Global, CN, and QoderWork editions) |
| **Pi** | `~/.pi/agent/sessions/` | Reconstructs active branch via last persisted parent chain, keeping earlier turns along the path |
| **Kimi Code** | `~/.kimi-code/sessions/` | Committed main-agent messages, deduplicating replay events |
| **Devin CLI** | `~/.local/share/devin/cli/sessions.db` | Read-only SQLite query along the active message branch |

> Adapters reflect observed macOS storage formats. Paths and formats may work on other Unix-like systems but are not yet verified across OSes, and client updates may change formats over time. Cloud-only web sessions, binary attachments, and separate subagent histories are outside completeness guarantees.

---

## Installation

Requires **Python 3.10+** with zero external runtime dependencies.

### 1. Clone and verify the CLI

```bash
git clone https://github.com/tt-a1i/pickup.git
cd pickup
python3 scripts/pickup.py --help
```

Optional shell alias in `~/.zshrc` or `~/.bashrc`:

```bash
alias pickup='python3 /absolute/path/to/pickup/scripts/pickup.py'
```

### 2. Install as an Agent Skill (Recommended)

Symlink the repository into your agent's skills directory so `/pickup` or natural-language requests trigger automatically:

```bash
# Example for Codex (same pattern works for Claude Code, Pi, OpenCode, etc.)
mkdir -p ~/.codex/skills
ln -s "$PWD" ~/.codex/skills/pickup
```

> Open a new session to refresh discovered skills. If a skill with the same name already exists at the target path, inspect it before replacing.

---

## Usage

Run from the repository root (or replace `python3 scripts/pickup.py` with `pickup` if aliased).

### 1. Find candidates

```bash
# List the 6 most recently updated sessions across all agents and directories
python3 scripts/pickup.py

# Search by verbatim quote, title, or topic keyword
python3 scripts/pickup.py "distinctive phrase from the session"

# Narrow to a specific agent when known
python3 scripts/pickup.py --agent kimi
python3 scripts/pickup.py --agent devin "login timeout"

# Customize limit or exclude the current session explicitly
python3 scripts/pickup.py --limit 10
python3 scripts/pickup.py --exclude '<full-session-id-or-uid>'
```

- **Discovery only — never auto-loads #1**: without a query, candidates sort by file modification time; with a query, matches in parsed titles, summaries, and dialogue outrank tool-output matches.
- Automatically excludes the active session when the caller runtime supplies its session ID; use `--exclude` otherwise.

### 2. Inspect overview & outline (`show`)

```bash
python3 scripts/pickup.py show '<uid>'
python3 scripts/pickup.py show '<uid>' --tail 2
```

Returns **every user prompt in the session (`Turn 1..N`)** as a compact outline, plus full details of the latest turns, tool call trails, and available errors or background task logs.

### 3. Read deeper on demand (`read`)

```bash
# Read dialogue from Turn 1 onwards (strips bulky raw stdout; paginates at 60,000 chars)
python3 scripts/pickup.py read '<uid>' --all

# Read a specific turn range spotted in the outline
python3 scripts/pickup.py read '<uid>' --turns 8-20

# Search across dialogue and retained tool records inside one session
python3 scripts/pickup.py read '<uid>' --grep 'build'

# Include raw tool output snippets
python3 scripts/pickup.py read '<uid>' --turns 8-20 --with-tools
```

### Using `/pickup` in chat

Once installed as a skill, just type in any new chat:

- **`/pickup`** — lists recent sessions across your machine so you can pick one.
- **`/pickup read the earlier discussion about login timeouts`** — locates and summarizes the matching thread.
- **Paste a quote or screenshot + `continue from here`** — the agent extracts a distinctive clue, loads the source session, checks live disk files / `git status` if coding, and picks up cleanly.

---

## Scope & Limitations

- **Strict local read-only**: opens SQLite with `?mode=ro`, spawns no daemons, builds no duplicate index DB, and makes zero network requests. (Content passed to an LLM by your host agent remains subject to that agent's data handling.)
- **Search latency**: full-history text search scans older files without a cap; narrow large histories with `--agent` or a specific phrase.
- **Pi branch resolution**: follows the parent chain from the last persisted node on disk; uncommitted in-memory branch switches cannot be inferred. Compaction summaries and summary-only sessions are preserved and searchable.
- **Devin CLI**: reads the local database only; does not access Devin's cloud web API or credentials.

---

## Development & Verification

```bash
python3 -m unittest discover -s tests -v
```

All **25 regression tests** run offline against temporary fixtures (no personal session data), covering candidate ranking, caller exclusion, ID disambiguation, Pi unique IDs & persisted branch resolution, summary-only sessions, Claude/Qoder array tool results, escaped CJK search, Antigravity full transcripts, Kimi replay deduplication, and Devin branch selection.

```text
pickup/
├── SKILL.md                  # Agent skill instructions
├── assets/cover.jpg          # README cover banner
├── scripts/
│   ├── pickup.py             # Core CLI: discovery, show, paginated read & adapters
│   ├── pickup_extra.py       # Kimi Code / Devin CLI readers
│   └── pickup_pi.py          # Pi persisted-branch resolver
└── tests/                    # 25 offline regression tests
```
