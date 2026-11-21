"""One dream run: gate, one agent session, post-run checks, report, state and the review mail. No LLM outside the session."""

import json
import shlex
import subprocess
import time
from pathlib import Path

from . import gate, review, session

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / "prompts" / "dream.md"

# bake, discard and mount (which retires overlay copies); applying staged changes is an "applied" event of origin "apply"
FORBIDDEN_EVENTS = {"baked", "discarded", "retired"}

MODE_RULES = {
    "stage": "`stage`。`ms write` 会自动暂存（不用加 `--stage`），改动进 staging/ 等用户审阅。"
             "暂存的改动不会出现在 `ms read` 里，要看用 `ms --json diff`；再写同一张卡时，是在它的暂存版本上改。",
    "direct": "`direct`。直接写入 @overlay；用户 bake 时会审阅这些改动。",
}
# the report's line on the changes: in stage mode the review mail lists them, numbered, with their reasons
REPORT_CHANGES = {
    "stage": "改了哪些卡不用逐条列，也不要给改动编号：审阅邮件会按程序的编号列出每处暂存改动和它的 reason；",
    "direct": "做了哪些修改（地址 + 一句话理由）；",
}


def check(events: list[dict], before: dict, after: dict, mode: str, exclude: tuple[str, ...]) -> tuple[list, list]:
    """Guardrail problems (the checkpoint stays) and concurrency warnings, from the events recorded during the run."""
    problems, warnings = [], []
    always = before["always"] | after["always"]
    for e in events:
        what = " ".join(str(x) for x in (e.get("event"), e.get("op"), e.get("address") or e.get("alias")) if x)
        if e.get("event") in FORBIDDEN_EVENTS or e.get("origin") == "apply":
            problems.append(f"不允许的操作：{what}")
        elif e.get("origin") != "write":
            continue  # the engine's own reference rewrites
        elif not gate.is_dream(e):
            warnings.append(f"其他会话同时写入：{what}（reason: {e.get('reason')}）")
        else:
            path = gate.card_path(e["address"])
            if e.get("op") == "delete":
                problems.append(f"dream 删除了卡片：{what}")
            if path.startswith(exclude):
                problems.append(f"dream 写了不归它管的卡：{what}")
            if e["event"] == "applied" and path in always:
                problems.append(f"常驻卡改动未经暂存：{what}")
            elif e["event"] == "applied" and mode == "stage":
                problems.append(f"stage 模式下未暂存的写入：{what}")
    problems += [f"validate 新增 error：{k}" for k in sorted(after["errors"] - before["errors"])]
    return problems, warnings


def repo_states(repos: list[Path]) -> dict:
    """HEAD, branch line (ahead / behind) and uncommitted changes of each watched repo. Outside the memory the session
    only reads (PLAN section 7), so all of it must be the same after the session."""
    def git(repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout

    return {str(r): (git(r, "rev-parse", "HEAD"), git(r, "status", "--porcelain", "-b"), git(r, "diff", "HEAD"))
            for r in repos}


def script_section(status: str, events: list[dict], problems: list, warnings: list, before: dict, after: dict) -> str:
    def change(key: str) -> str:
        return f"{before[key]} → {after[key]}"

    lines = ["", "---", "", "## 脚本检查", "",
             f"- 状态：{status}（{'checkpoint 已推进' if status == 'ok' else 'checkpoint 未推进，下次重来'}）",
             f"- 运行期间的 history 事件：{len(events)} 条，其中 dream 的写入 {sum(map(gate.is_dream, events))} 条",
             f"- 卡片：{change('cards')}；暂存待审：{change('pending')}",
             f"- 常驻卡：{len(before['always'])} → {len(after['always'])} 张，"
             f"{change('always_chars')} 字符（标题 + 摘要 + 正文）",
             f"- validate：error {len(before['errors'])} → {len(after['errors'])}，warning {change('warnings')}"]
    if problems:
        lines += ["", "**越界问题**", ""] + [f"- {p}" for p in problems]
    if warnings:
        lines += ["", "**并发警告**（这些写入留给下次运行）", ""] + [f"- {w}" for w in warnings]
    return "\n".join(lines) + "\n"


def run(cfg: dict, force: bool = False, dry_run: bool = False) -> dict:
    if dry_run:
        state = gate.load_state(cfg["paths"]["state"])
        return {"status": "dry-run", "input": gate.evaluate(cfg, state, gate.snapshot(cfg), force)}
    with session.locked(cfg):  # a mail poll in progress finishes first; none starts while the dream runs
        return dream(cfg, force)


def dream(cfg: dict, force: bool) -> dict:
    paths, ws, mode = cfg["paths"], cfg["memory"]["workspace"], cfg["dream"]["mode"]
    exclude = tuple(cfg["dream"]["exclude_prefixes"])
    state = gate.load_state(paths["state"])
    before = gate.snapshot(cfg)
    task = gate.evaluate(cfg, state, before, force)
    if not task["triggers"]:
        gate.save_state(paths["state"], gate.next_state(state, task, before) | {
            "last_run": {"status": "skipped", "at": session.now_iso()}})
        return {"status": "skipped"}

    for old in paths["runs"].glob("*/"):  # leftovers of a run killed before it could tidy up
        session.tidy(old)
    run_dir = session.new_run_dir(paths["runs"])
    input_path, report = run_dir / "input.json", run_dir / "report.md"
    input_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt = session.render(PROMPT.read_text(encoding="utf-8"), date=task["date"], workspace=ws, input=input_path,
                            report=report, ms=shlex.join([str(run_dir / session.GUARD), "-w", str(ws)]),
                            mode_rule=MODE_RULES[mode], changes=REPORT_CHANGES[mode], exclude="、".join(f"`{p}`" for p in exclude),
                            max_chars=cfg["dream"]["max_card_chars"])
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    started, t0, repos = session.now_iso(), time.time(), repo_states(cfg["repos"])
    res = session.launch(cfg, run_dir, prompt, write=True)
    events = gate.after(gate.history(ws), task["checkpoint"])
    after = gate.snapshot(cfg)
    problems, warnings = check(events, before, after, mode, exclude)
    problems += [f"仓库在运行期间变了（HEAD、分支状态或未提交的改动）：{repo}"
                 for repo, state in repo_states(cfg["repos"]).items() if state != repos[repo]]

    if (not report.exists() or report.stat().st_mtime < t0) and (text := session.final_text(res["output"])):
        report.write_text(text.rstrip() + "\n", encoding="utf-8")
    status = (res["status"] if res["status"] != "ok" else "no report" if not report.exists()
              else "guardrail problems" if problems else "ok")
    with open(report, "a", encoding="utf-8") as f:
        f.write(script_section(status, events, problems, warnings, before, after))
    latest = paths["runs"] / "latest.md"
    latest.unlink(missing_ok=True)
    latest.symlink_to(Path(run_dir.name) / "report.md")

    last_run = {"status": status, "at": started, "run_id": run_dir.name}
    gate.save_state(paths["state"], (gate.next_state(state, task, after) if status == "ok" else state) | {
        "last_run": last_run})
    engine = subprocess.run([cfg["memory"]["mindslice"], "--version"], capture_output=True, text=True).stdout.strip()
    meta = {"run_id": run_dir.name, "started": started, "finished": session.now_iso(), "status": status,
            "triggers": task["triggers"], "full_sweep": task["full_sweep"], "mode": mode, "engine": engine,
            "exit_code": res["exit_code"], "session_id": res["session_id"], "resume": res["resume"],
            "checkpoint": task["checkpoint"], "advanced": status == "ok", "problems": problems,
            "warnings": warnings, "run_dir": str(run_dir), "report": str(report), "cmd": res["cmd"],
            "mail": review.notify(cfg, run_dir.name, status, report)}
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta
