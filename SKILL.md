---
name: pickup
description: Find and read another local AI agent session across projects and tools. Use when the user invokes pickup or asks to find, read, or continue a previous session from a link, quote, title, topic, or screenshot.
---

# Pickup

Use `pickup` or the equivalent `python3 "<skill-directory>/scripts/pickup.py"`. Resolve `<skill-directory>` to the directory containing this file.

## Open the relevant passage

```bash
pickup read <uid> --turns 8-20
pickup show <id-or-link>
pickup read <uid> --grep '<keyword>'
```

With a known ID or link, open it directly. When a candidate gives a turn, run its `open match` command or use `read <uid> --turns X-Y` immediately. Use `show` for the outline when you need to choose turns. `--with-tools` adds brief tool output snippets. For exact commands, exit codes, tool call IDs, or subagent spawns, inspect `source_file` or use `export <uid> --format raw --output <new-path>` where raw events are available.

`show` and `read` default to 8,000 UTF-8 bytes per page, including `Next:` and `complete=`. Follow the `Next:` command when more of the selected view is needed. Capture the footer. If the host clips it, rerun with a smaller `--max-bytes`. Explicit `--max-chars` alone permits up to four bytes per character. Set both flags to enforce both limits. A stale cursor prints a `Restart with:` command for its original view.

Read depth follows the request. For the full decoded dialogue, use `read <uid> --all` through `complete=yes` or verify a Markdown export from `export <uid> --output <new-path.md>`. Add repeatable `--turns 8-10` to export selected ranges. A completed outline or grep view covers that view. Keep the original extension for raw archives such as `.jsonl.zst`. Devin raw export contains the selected session's nodes and active branch pointer.

## Find the session when needed

```bash
pickup '<distinctive quote, title, or topic>'
pickup
```

Search results and recent lists are candidates. Use the role, excerpt, and turn to choose. Keep the exact wording of a distinctive phrase from a quote or screenshot. Use `--agent` for a known source and `--project`, `--since`, or `--model` for supplied clues. If several records remain plausible, present the candidates. Without a clue, list recent sessions and ask the user to choose. Use the record UID from the ambiguity message when multiple files share an ID.

Open local Antigravity, Codex, Claude Code, and Qoder subagents directly by ID. Add `--include-subagents` to search or list them. Qoder reads follow the active branch. Text search and `read --grep` also find off-branch passages. Open those turns with `--all-branches --turns X`.

Pickup excludes a current session when its local ID is supplied by the caller. Use `--exclude <full-id-or-uid>` when the environment has no ID. For Claude Code, a caller can supply `CLAUDE_SESSION_ID` from the documented hook input `session_id`. [Claude Code hooks](https://code.claude.com/docs/en/hooks#common-input-fields)

Check stderr coverage and fidelity labels. Antigravity can fall back to SQLite indexed text and Codex to SQLite projected dialogue when raw transcripts are missing. These sources support search, read, and Markdown export. For raw events and tool details, a raw transcript is required. Metadata-only results can still identify a session by title or ID.

## Continue the current task

Attribute historical claims to the earlier session and verify live state before acting. For code continuation, check live `git status` and `git diff` in the target workspace. When the request is only to pick up a session, identify the selected record and its last verified state.
