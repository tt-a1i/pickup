"""Select the persisted active context from a Pi session file."""


def select_entries(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Return the active history, retaining the session header and context summaries.

    Pi session-manager.js _buildIndex restores the last persisted node as the
    leaf on load. Older version 1 files have no IDs and form a linear history.
    Keep pre-compaction history because pickup reads history, not model context.
    """
    headers = [entry for entry in entries if entry.get("type") == "session"]
    nodes = [entry for entry in entries if entry.get("type") != "session"]
    if not nodes:
        return headers, []
    notes = []
    if not any(entry.get("id") for entry in nodes):
        path = nodes
    else:
        by_id = {}
        for entry in nodes:
            node_id = entry.get("id")
            if isinstance(node_id, str) and node_id:
                if node_id in by_id:
                    return headers, ["Pi session has duplicate node IDs; the active branch cannot be determined."]
                by_id[node_id] = entry
        leaf = nodes[-1].get("id")
        if not isinstance(leaf, str) or leaf not in by_id:
            return headers, ["Pi session has a node without an ID; the active branch cannot be determined."]
        path = []
        seen = set()
        while leaf is not None:
            if not isinstance(leaf, str) or leaf not in by_id or leaf in seen:
                return headers, ["Pi session has a broken or cyclic parent chain; the active branch cannot be determined."]
            seen.add(leaf)
            entry = by_id[leaf]
            path.append(entry)
            leaf = entry.get("parentId")
        path.reverse()

    for entry in path:
        if entry.get("type") in ("compaction", "branch_summary") and entry.get("summary"):
            label = "Pi compaction summary" if entry["type"] == "compaction" else "Pi branch summary"
            notes.append(f"{label}: {entry['summary']}")
    return headers + path, notes
