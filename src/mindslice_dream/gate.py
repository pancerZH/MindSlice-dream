"""The gate: decide without an LLM whether tonight needs a dream session, and what it should look at."""

import json
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

# reasons of the dream session's writes and of the patches the email review makes for the user: neither triggers a run
DREAM_PREFIXES = ("[dream]", "[dream-review]")


def mindslice_json(cfg: dict, *args: str) -> dict:
    """One `mindslice --json` call on the configured workspace (validate exits 1 on errors but still prints JSON)."""
    res = subprocess.run([cfg["memory"]["mindslice"], "-w", str(cfg["memory"]["workspace"]), "--json", *args],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"mindslice {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip()}") from None


def history(ws: Path) -> list[dict]:
    """The complete events of .history/changes.jsonl, read directly and never written."""
    path = ws / ".history" / "changes.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")[:-1]  # drops "" or an interrupted last line
    return [json.loads(line) for line in lines if line.strip()]


def after(events: list[dict], checkpoint: dict | None) -> list[dict]:
    """Events recorded after the checkpoint: found by event_id, else compared by timestamp."""
    if not checkpoint:
        return events
    ids = [e["event_id"] for e in events]
    if checkpoint["event_id"] in ids:
        return events[ids.index(checkpoint["event_id"]) + 1:]
    since = datetime.fromisoformat(checkpoint["timestamp"])
    return [e for e in events if datetime.fromisoformat(e["timestamp"]) > since]


def position(events: list[dict], fallback: dict | None = None) -> dict | None:
    """The history position after the last event, as a checkpoint."""
    return {"event_id": events[-1]["event_id"], "timestamp": events[-1]["timestamp"]} if events else fallback


def is_dream(e: dict) -> bool:
    return (e.get("reason") or "").startswith(DREAM_PREFIXES)


def card_path(address: str) -> str:
    """'@overlay/projects/x' -> 'projects/x'."""
    return address.split("/", 1)[1] if address.startswith("@") and "/" in address else address


def issue_key(issue: dict) -> str:
    return f"{issue['severity']} {issue['code']} {issue.get('address') or issue.get('file')}: {issue['message']}"


def snapshot(cfg: dict) -> dict:
    """What the gate and the post-run checks look at: validate issues, always-loaded cards, sizes."""
    report = mindslice_json(cfg, "validate")
    always = mindslice_json(cfg, "manifest", "--limit", "0").get("always_loaded", [])
    return {
        "issues": report["errors"] + report["warnings"],
        "errors": {issue_key(i) for i in report["errors"]},
        "warnings": len(report["warnings"]),
        "always": {card_path(c["address"]) for c in always},
        "cards": report["stats"]["cards"],
        "pending": mindslice_json(cfg, "status")["memory_state"]["pending_changes"],
        "always_chars": sum(len(c.get(k) or "") for c in always for k in ("title", "summary", "body")),
    }


def head(repo: Path) -> str | None:
    res = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None


def evaluate(cfg: dict, state: dict, snap: dict, force: bool = False, now: datetime | None = None) -> dict:
    """Tonight's task (the content of input.json); `triggers` is empty when there is nothing to do."""
    now = now or datetime.now().astimezone()
    dream = cfg["dream"]
    events = history(cfg["memory"]["workspace"])
    checkpoint = state.get("checkpoint")
    if checkpoint:
        new = after(events, checkpoint)
    else:  # first run: only recent writes count as new
        cutoff = now - timedelta(days=dream["first_run_days"])
        new = [e for e in events if datetime.fromisoformat(e["timestamp"]) >= cutoff]

    # keyed by card path: a card written in @overlay may have been baked into a slice since
    writes: dict[str, list[dict]] = {}
    exclude = tuple(dream["exclude_prefixes"])
    for e in new:
        path = card_path(e.get("address", ""))
        if e.get("origin") == "write" and not is_dream(e) and not path.startswith(exclude):
            writes.setdefault(path, []).append({k: e.get(k) for k in ("timestamp", "event", "op", "reason")})
    writes = {path: w for path, w in writes.items() if w[-1]["op"] != "delete"}  # gone: nothing left to check

    seen = set(state.get("validate", []))
    new_issues = [i for i in snap["issues"] if issue_key(i) not in seen]

    old_heads = state.get("heads", {})
    repos = []
    for repo in cfg["repos"]:
        current, previous = head(repo), old_heads.get(str(repo))
        repos.append({"path": str(repo), "head": current, "previous": previous,
                      "changed": previous is not None and current != previous})

    last_sweep = state.get("last_full_sweep")
    full_sweep = not last_sweep or (now.date() - date.fromisoformat(last_sweep)).days >= dream["full_sweep_days"]

    hits = {"new_writes": bool(writes), "validate": bool(new_issues), "repo_head": any(r["changed"] for r in repos),
            "full_sweep": full_sweep, "force": force}
    return {
        "date": now.date().isoformat(),
        "workspace": str(cfg["memory"]["workspace"]),
        "mode": dream["mode"],
        "triggers": [name for name, hit in hits.items() if hit],
        "full_sweep": full_sweep,
        "new_writes": [{"card": path, "writes": w} for path, w in writes.items()],
        "validate_new": new_issues,
        "repos": repos,
        "checkpoint": position(events, checkpoint),
    }


def next_state(state: dict, task: dict, snap: dict) -> dict:
    """The state after a successful run (or a skipped night): the checkpoint moves to the task's position."""
    return {**state,
            "checkpoint": task["checkpoint"],
            "validate": sorted(issue_key(i) for i in snap["issues"]),
            "heads": {r["path"]: r["head"] for r in task["repos"] if r["head"]},
            "last_full_sweep": task["date"] if task["full_sweep"] else state.get("last_full_sweep")}


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
