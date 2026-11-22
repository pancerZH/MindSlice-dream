"""One harness session run cleanly: its environment, its run directory, and nothing left behind afterwards."""

import fcntl
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# what a run directory keeps; the session's scratch files in work/ (and anything else) are deleted after the run
RUN_RECORD = {"input.json", "prompt.md", "stdout.log", "stderr.log", "report.md", "drops.json", "meta.json"}

# variables a parent harness leaks into children; without them the child starts a standalone session
STRIP_PREFIXES = ("ANTIGRAVITY_", "CLAUDE_CODE_", "CODEX_", "MINDSLICE_PLUGIN_", "PLUGIN_")
STRIP_NAMES = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT"}
SESSION_KEYS = ("session_id", "conversation_id", "thread_id", "sessionId", "conversationId")
GUARD = Path("bin") / "mindslice"  # in the run directory; the session's only mindslice (guard.py)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def locked(cfg: dict, wait: bool = True):
    """The lock the nightly run and the mail poll share; yields False when it is taken and wait is False."""
    path = cfg["paths"]["runs"] / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def clean_env(workspace: Path, tag: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_NAMES and not k.startswith(STRIP_PREFIXES)}
    # ISOLATE: the harness's own MindSlice plugin (its own engine version, its own configured workspace) loads no memory
    # and offers no MCP tools, so the session can reach memory only through `ms`; a call without -w still hits WORKSPACE.
    # RUN tags every process of the session (see stop_leftovers); no bytecode caches are left in the repos it tests.
    return env | {"MINDSLICE_PLUGIN_ISOLATE": "1", "MINDSLICE_WORKSPACE": str(workspace),
                  "MINDSLICE_DREAM_RUN": tag, "PYTHONDONTWRITEBYTECODE": "1"}


def install_guard(cfg: dict, run_dir: Path, write: bool) -> None:
    """The `mindslice` the session gets, first on its PATH and named in the dream prompt: guard.py run by this
    Python, which has the package and the engine, then the configured CLI. Deleted with the run's other files."""
    policy = {"cli": str(cfg["memory"]["mindslice"]), "write": write, "stage": cfg["dream"]["mode"] == "stage",
              "exclude": list(cfg["dream"]["exclude_prefixes"])}
    path = run_dir / GUARD
    path.parent.mkdir()
    path.write_text(f"#!{sys.executable}\nimport sys\n\nfrom mindslice_dream.guard import main\n\n"
                    f"sys.exit(main(sys.argv[1:], {policy!r}))\n")
    path.chmod(0o755)


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


def launch(cfg: dict, run_dir: Path, prompt: str, timeout_minutes: float | None = None, write: bool = False) -> dict:
    """One non-interactive harness session working in run_dir/work, with its output in stdout.log / stderr.log.
    Its mindslice is the guard, which lets it write only when write is true.

    Afterwards, timed out or not, every process it left running is stopped and run_dir keeps only the run record."""
    h = cfg["harness"]
    work = run_dir / "work"
    work.mkdir()
    install_guard(cfg, run_dir, write)
    env = clean_env(cfg["memory"]["workspace"], str(run_dir))
    env["PATH"] = os.pathsep.join([str((run_dir / GUARD).parent), env.get("PATH", "")])
    exit_code, status, proc = None, "ok", None
    with open(run_dir / "stdout.log", "w") as out, open(run_dir / "stderr.log", "w") as err:
        try:
            proc = subprocess.Popen([a.replace("{prompt}", prompt) for a in h["cmd"]], cwd=work, env=env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            exit_code = proc.wait(timeout=(timeout_minutes or h["timeout_minutes"]) * 60)
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


def new_run_dir(runs: Path, suffix: str = "") -> Path:
    base = datetime.now().strftime("%Y%m%d-%H%M%S") + suffix
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
