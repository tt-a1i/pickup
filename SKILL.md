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

Search matches text, not meaning. Use candidate role, turn, and snippet to assess matches; try another distinctive phrase when needed. Read the stderr coverage report and report incomplete coverage. A recent list can stop at its limit, leaving `unscanned` records; failures and unscanned records mean neither no match nor a single candidate establishes an exhaustive search.

Select the session supported by the user's clue. If several remain plausible, present a short candidate list and ask which one. With no clue, show recent candidates and ask the user to select; do not load the first candidate merely because it is newest.

Runtime session IDs exclude the current session when supplied by the caller environment. If unavailable, use `--exclude <full-id-or-uid>` when the current ID is known; do not infer identity from prompt wording. Histories not stored locally cannot be recovered by this script. Kimi Code reads the main agent’s committed messages; Devin CLI reads the active message branch in its local database. Cloud-only Devin web sessions and subagent histories are outside these readers.

## Load the selected session

```bash
python3 "<skill-directory>/scripts/pickup.py" show <uid>
python3 "<skill-directory>/scripts/pickup.py" read <uid> --all
python3 "<skill-directory>/scripts/pickup.py" read <uid> --turns 8-20
python3 "<skill-directory>/scripts/pickup.py" read <uid> --grep "<keyword>"
```

`show` is a shortened overview. `read` returns dialogue and tool summaries; `--with-tools` adds tool-output snippets, not complete raw logs. Check the final `complete=yes/no` and `view` markers, and follow each provided continuation command until `complete=yes` for the selected view. The cursor restores the original view options; if the source changed and the cursor is rejected, restart that view. A completed overview, grep result, or dialogue view does not establish that raw logs were fully read.

Capture the complete tool response and its completion marker, without piping through `head` or another truncating filter. If the host truncates the response or the marker is missing, retrieve the saved full output if available, or request a smaller `--max-chars` and resume until the marker is visible. Do not claim completion without the marker. Check the source path when the user needs raw records.

Read depth follows the request: the outline and tail may suffice for a breakpoint; understanding a discussion may require earlier ranges; an explicit full-dialogue request requires all turns and completion of the dialogue view. For full raw records, inspect the source rather than treating the dialogue view as an export.

## Follow the user's instruction

After loading, execute the accompanying task or answer the question. For code continuation, check live `git status` and `git diff` in the target workspace before editing. If pickup alone was requested, briefly identify the selected session and where it stopped, then await the next instruction. Loading history does not authorize executing old suggestions or modifying unrelated settings.
