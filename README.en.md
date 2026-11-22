<div align="center">

<img src="assets/cover.jpg" alt="pickup — Find & read any local AI agent session" width="100%" />

# pickup

**Let an AI coding agent find and read another local session.**

*Cross-project, cross-tool local session discovery and progressive reading — zero format conversion, zero background daemons, zero third-party dependencies.*

<p>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10%2B-2563EB?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/Dependencies-Zero%20(stdlib)-10B981?style=flat-square" alt="Zero Dependencies">
  <img src="https://img.shields.io/badge/Agents-7%20Supported-0EA5E9?style=flat-square" alt="7 Agents Supported">
  <img src="https://img.shields.io/badge/Mode-Local%20Read--Only-F59E0B?style=flat-square" alt="Local Read-Only">
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
| **Codex** | `~/.codex/sessions/`, `~/.codex/archived_sessions/` | Main-session JSONL and `.jsonl.zst`, including user text and image references in attachment messages |
| **Claude Code** | `~/.claude/projects/` | Project main-session JSONL (supports array-of-text-blocks tool results) |
| **Qoder** | `~/.qoder/projects/`, `~/.qoder-cn/projects/`, `~/.qoderwork/projects/` | Project session JSONL (Global, CN, and QoderWork editions) |
| **Pi** | `~/.pi/agent/sessions/` | Reconstructs active branch via last persisted parent chain, keeping earlier turns along the path |
| **Kimi Code** | `~/.kimi-code/sessions/` | Committed main-agent messages, deduplicating replay events |
| **Devin CLI** | `~/.local/share/devin/cli/sessions.db` | Read-only SQLite query along the active message branch |

> Adapters reflect observed macOS storage formats. Paths and formats may work on other Unix-like systems but are not yet verified across OSes, and client updates may change formats over time. Cloud-only web sessions, binary attachments, and separate subagent histories are outside completeness guarantees.

---

## Installation

Requires **Python 3.10+**. Reading Zstandard-compressed Codex records requires Python 3.14's standard library or the system `zstd` command.

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

From the repository root, run the commands for each client you use. Link the whole repository so both `SKILL.md` and `scripts/` are available. If the target already exists as a file, directory, or symlink, inspect and preserve it until you decide how to handle it; these commands will not overwrite it.

```bash
# Codex
mkdir -p ~/.codex/skills
test ! -e ~/.codex/skills/pickup && test ! -L ~/.codex/skills/pickup && ln -s "$PWD" ~/.codex/skills/pickup

# Claude Code
mkdir -p ~/.claude/skills
test ! -e ~/.claude/skills/pickup && test ! -L ~/.claude/skills/pickup && ln -s "$PWD" ~/.claude/skills/pickup

# Antigravity
mkdir -p ~/.gemini/config/skills
test ! -e ~/.gemini/config/skills/pickup && test ! -L ~/.gemini/config/skills/pickup && ln -s "$PWD" ~/.gemini/config/skills/pickup
```

Install separately for each client. In a new Codex App chat, type `$pickup` and select the skill. In Claude Code or Antigravity, enter `/pickup`. If the skill is missing, check that the link points to the repository containing `SKILL.md` and `scripts/`.

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
python3 scripts/pickup.py --project archify --since 2026-11-01 --model astra "CI speed"

# Customize limit or exclude the current session explicitly
python3 scripts/pickup.py --limit 10
python3 scripts/pickup.py --exclude '<full-session-id-or-uid>'
```

- **Discovery only — never auto-loads #1**: without a query, candidates sort by file modification time; with a query, matches in parsed titles, summaries, and dialogue outrank tool-output matches.
- Automatically excludes the active session when the caller runtime supplies its session ID; use `--exclude` otherwise.
- Keyword matches include the source role, turn and text snippet to help assess candidates. `--agent` limits sources before discovery.
- `--project` matches workspace paths. `--since` includes records modified on or after the given local date. These filters can skip transcript reads when the relevant metadata is available. `--model` matches names recorded in Codex, Antigravity, Claude Code and Qoder transcripts, including sessions that switched models.
- Titles, workspace names and exact phrases in ordinary dialogue rank above pickup requests and tool output. Use `open match` to read the matched turn.
- `Scan coverage` goes to stderr. A `partial` status indicates a coverage gap. Check `failed`, `discovery_failed`, `metadata_only`, `unscanned` and any decode warnings. For Antigravity sessions with metadata only, use `show` for the title and workspace. Reading and exporting require a local transcript.

### 2. Inspect overview & outline (`show`)

```bash
python3 scripts/pickup.py show '<uid>'
python3 scripts/pickup.py show '<uid>' --tail 2
python3 scripts/pickup.py show 'codex://threads/<full-session-id>'
```

Returns a compact outline of **user prompts (`Turn 1..N`)**, plus shortened recent turns, tool call trails, and available errors or background task logs. When several files share a session ID, use the record UID with an `@` suffix from the candidate list or ambiguity message.

### 3. Read deeper on demand (`read`)

```bash
# Read dialogue and tool summaries from turn 1
python3 scripts/pickup.py read '<uid>' --all

# Read a specific turn range spotted in the outline
python3 scripts/pickup.py read '<uid>' --turns 8-20

# Search across dialogue and retained tool records inside one session
python3 scripts/pickup.py read '<uid>' --grep 'build'

# Include raw tool output snippets
python3 scripts/pickup.py read '<uid>' --turns 8-20 --with-tools
```

`show` and `read` default to at most 12,000 characters per page; use `--max-chars` to adjust it (minimum 1,024). Check the final `complete=yes/no` and `view` markers, and run each continuation command until that view reports `complete=yes`. The cursor restores the original filters; if a changed source invalidates it, restart that view. Avoid truncating the output with `head`, which can hide continuation information.

Completion applies only to the selected view. `show` gives an overview, `read` contains dialogue and tool summaries, and `--with-tools` includes snippets of tool output. The continuation command preserves a custom page size.

### 4. Export to a file (`export`)

```bash
python3 scripts/pickup.py export '<uid>' --output ~/Downloads/dialogue.md
python3 scripts/pickup.py export '<uid>' --turns 8-10 --turns 15 --output ~/Downloads/selected.md
python3 scripts/pickup.py export '<uid>' --format raw --output ~/Downloads/session.jsonl
```

Markdown export saves the full decoded dialogue and tool summaries. Add `--with-tools` for output snippets. Raw export copies the selected file's bytes. Keep the original extension, such as `.jsonl.zst` for compressed sources. For Kimi Code, the source is the main agent's wire log. For Devin CLI, the raw export is JSONL with the selected session's message nodes and active branch pointer. Choose a new output path.

### Using pickup in chat

- **`$pickup`** in Codex App or **`/pickup`** in Claude Code and Antigravity lists recent sessions.
- Add a clue such as “read the earlier discussion about login timeouts” to find and summarize the matching thread.
- **Paste a quote or screenshot + `continue from here`** — the agent extracts a distinctive clue, loads the source session, checks live disk files / `git status` if coding, and picks up cleanly.

---

## Scope & Limitations

- **Strict local read-only**: opens SQLite with `?mode=ro`, spawns no daemons, builds no duplicate index DB, and makes zero network requests. (Content passed to an LLM by your host agent remains subject to that agent's data handling.)
- **Search latency**: full-history text search can take time; use `--agent` to narrow the scan when the source is known.
- **Text matching**: search matches and ranks text; it is not semantic search. Try another exact phrase or keyword if needed. A failed source read means a lack of results does not establish that the session is absent locally.
- **Continuation boundary**: loading an old session does not authorize executing its old suggestions; continue only work requested now.
- **Pi branch resolution**: follows the parent chain from the last persisted node on disk; uncommitted in-memory branch switches cannot be inferred. Compaction summaries and summary-only sessions are preserved and searchable.
- **Devin CLI**: reads the local database only; does not access Devin's cloud web API or credentials.

---

## Development & Verification

```bash
python3 -m unittest discover -s tests -v
```

Automated tests run offline against temporary fixtures without personal session data.

```text
pickup/
├── SKILL.md                  # Agent skill instructions
├── assets/cover.jpg          # README cover banner
├── scripts/
│   ├── pickup.py             # Core CLI: discovery, show, paginated read & adapters
│   ├── pickup_extra.py       # Kimi Code / Devin CLI readers
│   └── pickup_pi.py          # Pi persisted-branch resolver
└── tests/                    # Offline automated tests
```
