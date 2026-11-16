---
name: pickup
description: Find and read another local AI agent session across projects and tools, including Kimi Code and Devin CLI. Use for /pickup or requests to read or continue a previous session, with an optional quote, title, topic, or screenshot.
---

# Pickup

Locate local histories across Antigravity, Codex, Claude Code, Qoder, Pi, Kimi Code, and Devin CLI, across directories. Source formats and available options are listed by `pickup --help`. Resolve `<skill-directory>` to the directory containing this SKILL.md. Use its Python entrypoint if the `pickup` command is unavailable.

## Find and select

```bash
python3 "<skill-directory>/scripts/pickup.py"
python3 "<skill-directory>/scripts/pickup.py" "<distinctive phrase, title, or topic>"
```

Both commands return candidates only. If the source is already known, `--agent kimi` or `--agent devin` narrows the search; otherwise keep the cross-tool search. Full-history text searches can be slower than recent-session listings. Without a clue, sessions are sorted by file modification time; this is a discovery hint, not proof of user intent. With a clue, literal matches in titles and dialogue rank above tool-output matches. Extract a distinctive phrase from a screenshot or pasted output; the user need not identify the source app or directory.

Select the session supported by the user's clue. If several remain plausible, present a short candidate list and ask which one. With no clue, show recent candidates and ask the user to select; do not load the first candidate merely because it is newest.

Runtime session IDs exclude the current session when supplied by the caller environment. If unavailable, use `--exclude <full-id-or-uid>` when the current ID is known; do not infer identity from prompt wording. Histories not stored locally cannot be recovered by this script. Kimi Code reads the main agent’s committed messages; Devin CLI reads the active message branch in its local database. Cloud-only Devin web sessions and subagent histories are outside these readers.

## Load the selected session

```bash
python3 "<skill-directory>/scripts/pickup.py" show <uid>
python3 "<skill-directory>/scripts/pickup.py" read <uid> --all
python3 "<skill-directory>/scripts/pickup.py" read <uid> --turns 8-20
python3 "<skill-directory>/scripts/pickup.py" read <uid> --grep "<keyword>"
```

`show` provides every user-prompt outline entry and the latest four turns, with compact tool history and available errors/background logs. Outline entries and tail text are shortened; use `read` for full dialogue. `--all` omits bulky raw tool output and paginates at turn boundaries. Follow every continuation range until the requested reading is complete. If the tool response itself truncates, request smaller turn ranges to recover the missing portion. `--with-tools` adds tool-output snippets; `--grep` also searches retained outputs.

Read depth follows the request: the outline and tail may suffice for a breakpoint; understanding a discussion may require earlier ranges; an explicit full-read request requires all turns.

## Follow the user's instruction

After loading, execute the accompanying task or answer the question. For code continuation, check live `git status` and `git diff` in the target workspace before editing. If pickup alone was requested, briefly identify the selected session and where it stopped, then await the next instruction. Loading history does not authorize executing old suggestions or modifying unrelated settings.
