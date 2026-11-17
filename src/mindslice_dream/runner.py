"""One dream run: gate, one agent session, post-run checks, report and state. No LLM outside the session."""

import itertools
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gate

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / "prompts" / "dream.md"

# bake, discard and mount (which retires overlay copies); applying staged changes is an "applied" event of origin "apply"
FORBIDDEN_EVENTS = {"baked", "discarded", "retired"}

# what a run directory keeps; the session's scratch files in work/ (and anything else) are deleted after the run
RUN_RECORD = {"input.json", "prompt.md", "stdout.log", "stderr.log", "report.md", "meta.json"}

MODE_RULES = {
    "stage": "`stage`。每次写入都加 `--stage`，改动进 staging/ 等用户审阅（带 `--also` 时只在这次调用上加）。"
             "暂存的改动不会出现在 `ms read` 里，要看用 `ms --json diff`；再写同一张卡时，是在它的暂存版本上改。",
    "direct": "`direct`。直接写入 @overlay，不加 `--stage`；用户 bake 时会审阅这些改动。",
}

# variables a parent harness leaks into children; without them the child starts a standalone session
STRIP_PREFIXES = ("ANTIGRAVITY_", "CLAUDE_CODE_", "CODEX_", "MINDSLICE_PLUGIN_", "PLUGIN_")
STRIP_NAMES = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT"}
SESSION_KEYS = ("session_id", "conversation_id", "thread_id", "sessionId", "conversationId")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_env(workspace: Path, tag: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_NAMES and not k.startswith(STRIP_PREFIXES)}
    # ISOLATE: the harness's own MindSlice plugin (its own engine version, its own configured workspace) loads no memory
    # and offers no MCP tools, so the session can reach memory only through `ms`; a call without -w still hits WORKSPACE.
    # RUN tags every process of the session (see stop_leftovers); no bytecode caches are left in the repos it tests.
    return env | {"MINDSLICE_PLUGIN_ISOLATE": "1", "MINDSLICE_WORKSPACE": str(workspace),
                  "MINDSLICE_DREAM_RUN": tag, "PYTHONDONTWRITEBYTECODE": "1"}


def tagged(tag: str) -> list[int]:
    """Processes whose environment carries this run's tag, whatever process group or session they moved to."""
    needle = f"MINDSLICE_DREAM_RUN={tag}".encode()
    pids = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if needle in (proc / "environ").read_bytes().split(b"\0"):
                pids.append(int(proc.name))
        except OSError:  # gone, a zombie, or not ours
            continue
    return pids


def stop_leftovers(tag: str) -> None:
    """Stop every process the session started and left running: SIGTERM, then SIGKILL after a grace period."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in tagged(tag):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        for _ in range(20):
            if not tagged(tag):
                return
            time.sleep(0.1)


def tidy(run_dir: Path) -> None:
    """Leave only the run record in a run directory: the session's work/ and anything else it wrote go."""
    for path in run_dir.iterdir():
        if path.name in RUN_RECORD:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def json_values(text: str):
    """Every JSON value in the output: the whole text, else one per line (stream-json / NDJSON)."""
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        if line.strip().startswith(("{", "[")):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def find_key(value, keys):
    if isinstance(value, dict):
        for k in keys:
            if isinstance(value.get(k), str) and value[k]:
                return value[k]
        value = list(value.values())
    if isinstance(value, list):
        for v in value:
            if found := find_key(v, keys):
                return found
    return None


def find_session_id(output: str) -> str | None:
    return next((s for v in json_values(output) if (s := find_key(v, SESSION_KEYS))), None)


def final_text(output: str) -> str | None:
    """The harness's final answer: agy `response`, claude `result`."""
    text = None
    for value in json_values(output):
        if isinstance(value, dict):
            text = next((value[k] for k in ("response", "result") if isinstance(value.get(k), str)), text)
    return text


def launch(cfg: dict, run_dir: Path, prompt: str) -> dict:
    """One non-interactive harness session working in run_dir/work, with its output in stdout.log / stderr.log.

    Afterwards, timed out or not, every process it left running is stopped and run_dir keeps only the run record."""
    h = cfg["harness"]
    work = run_dir / "work"
    work.mkdir()
    exit_code, status, proc = None, "ok", None
    with open(run_dir / "stdout.log", "w") as out, open(run_dir / "stderr.log", "w") as err:
        try:
            proc = subprocess.Popen([a.replace("{prompt}", prompt) for a in h["cmd"]], cwd=work,
                                    env=clean_env(cfg["memory"]["workspace"], str(run_dir)), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err)
            exit_code = proc.wait(timeout=h["timeout_minutes"] * 60)
        except subprocess.TimeoutExpired:
            status = "timeout"
        except FileNotFoundError as e:
            status = f"harness not found: {e}"
        finally:  # also on Ctrl-C or a crash in the runner
            stop_leftovers(str(run_dir))
            if proc:
                proc.wait()
            tidy(run_dir)
    if status == "ok" and exit_code != 0:
        status = f"harness exit code {exit_code}"
    output = (run_dir / "stdout.log").read_text(errors="replace")
    session_id = find_session_id(output)
    return {"exit_code": exit_code, "status": status, "output": output, "session_id": session_id,
            "resume": h["resume"].format(session_id=session_id) if session_id else None,
            "cmd": [a if "{prompt}" not in a else a.replace("{prompt}", "<prompt.md>") for a in h["cmd"]]}


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


def new_run_dir(runs: Path) -> Path:
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i in itertools.count(1):
        run_dir = runs / (base if i == 1 else f"{base}-{i}")
        try:
            run_dir.mkdir(parents=True)
            return run_dir
        except FileExistsError:
            continue


def render(template: str, **values) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def run(cfg: dict, force: bool = False, dry_run: bool = False) -> dict:
    paths, ws, mode = cfg["paths"], cfg["memory"]["workspace"], cfg["dream"]["mode"]
    exclude = tuple(cfg["dream"]["exclude_prefixes"])
    state = gate.load_state(paths["state"])
    before = gate.snapshot(cfg)
    task = gate.evaluate(cfg, state, before, force)
    if dry_run:
        return {"status": "dry-run", "input": task}
    if not task["triggers"]:
        gate.save_state(paths["state"], gate.next_state(state, task, before) | {
            "last_run": {"status": "skipped", "at": now_iso()}})
        return {"status": "skipped"}

    for old in paths["runs"].glob("*/"):  # leftovers of a run killed before it could tidy up
        tidy(old)
    run_dir = new_run_dir(paths["runs"])
    input_path, report = run_dir / "input.json", run_dir / "report.md"
    input_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prompt = render(PROMPT.read_text(encoding="utf-8"), date=task["date"], workspace=ws, input=input_path,
                    report=report, ms=shlex.join([cfg["memory"]["mindslice"], "-w", str(ws)]),
                    mode_rule=MODE_RULES[mode], exclude="、".join(f"`{p}`" for p in exclude))
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    started, t0 = now_iso(), time.time()
    res = launch(cfg, run_dir, prompt)
    events = gate.after(gate.history(ws), task["checkpoint"])
    after = gate.snapshot(cfg)
    problems, warnings = check(events, before, after, mode, exclude)

    if (not report.exists() or report.stat().st_mtime < t0) and (text := final_text(res["output"])):
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
    meta = {"run_id": run_dir.name, "started": started, "finished": now_iso(), "status": status,
            "triggers": task["triggers"], "full_sweep": task["full_sweep"], "mode": mode, "engine": engine,
            "exit_code": res["exit_code"], "session_id": res["session_id"], "resume": res["resume"],
            "checkpoint": task["checkpoint"], "advanced": status == "ok", "problems": problems,
            "warnings": warnings, "run_dir": str(run_dir), "report": str(report), "cmd": res["cmd"]}
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta
