import json
import os
import time
from pathlib import Path

import pytest

from conftest import ROOT, ms
from mindslice_dream import cli, gate, runner, session


def gone(pid: int, wait: float = 3.0) -> bool:
    """The process no longer runs (a zombie waiting for its new parent counts as gone)."""
    for _ in range(int(wait * 10)):
        try:
            if Path(f"/proc/{pid}/stat").read_text().split(") ")[1].startswith("Z"):
                return True
        except FileNotFoundError:
            return True
        time.sleep(0.1)
    return False


def session_output(meta: dict) -> list[dict]:
    return [json.loads(line) for line in (Path(meta["run_dir"]) / "stdout.log").read_text().splitlines() if line]


def test_ok_run_advances_the_checkpoint(cfg, ws):
    start = gate.history(ws)[-1]["event_id"]
    meta = runner.run(cfg)
    assert meta["status"] == "ok" and meta["advanced"] and meta["problems"] == [] and meta["warnings"] == []
    assert meta["engine"].startswith("mindslice ")
    assert meta["resume"] == "agy --conversation fake-dream-1"

    state = gate.load_state(cfg["paths"]["state"])
    assert state["checkpoint"]["event_id"] == start  # the position when the run started
    events = gate.after(gate.history(ws), state["checkpoint"])
    assert [(e["event"], e["op"]) for e in events] == [("proposed", "patch"), ("proposed", "append"),
                                                       ("proposed", "create")]  # the --also batch is one call

    run_dir = Path(meta["run_dir"])
    # only the run record stays: the session's work/ (batch.json) and the stray notes.txt are gone
    assert {p.name for p in run_dir.iterdir()} == session.RUN_RECORD
    report = (cfg["paths"]["runs"] / "latest.md").read_text()
    assert report.startswith("# Dream 报告") and "## 脚本检查" in report and "checkpoint 已推进" in report
    prompt = (run_dir / "prompt.md").read_text()
    guard = str(run_dir / session.GUARD)
    assert "{{" not in prompt and f"`{guard} -w {ws}`" in prompt and runner.REPORT_CHANGES["stage"] in prompt
    # the harness's own MindSlice plugin is switched off; memory is reachable only through the configured workspace,
    # and the session's mindslice is the guard, which staged the writes the session sent without --stage
    [seen] = session_output(meta)
    assert seen["cwd"] == str(run_dir / "work")
    assert seen["ms"] == seen["path_mindslice"] == guard
    assert seen["env"] == {"MINDSLICE_PLUGIN_ISOLATE": "1", "MINDSLICE_WORKSPACE": str(ws), "PYTHONDONTWRITEBYTECODE": "1"}


@pytest.mark.parametrize("mode, why", [
    ("delete", "dream 不删除卡：projects/demo/status"),
    ("invest", "invest/theses/x 由别的流程审阅，dream 不写"),
    ("apply", "这个会话不能用 `mindslice apply`，只能用读记忆的命令和 `write`"),
    ("nostage", "不能用 `--no-stage`"),
    ("noreason", "projects/demo/status：reason 要以 `[dream]` 开头"),
    ("alsodelete", "dream 不删除卡：projects/demo/notes"),
])
def test_the_guard_refuses_and_nothing_is_written(cfg, ws, monkeypatch, mode, why):
    start = gate.history(ws)[-1]["event_id"]
    monkeypatch.setenv("FAKE_DREAM", mode)
    meta = runner.run(cfg)
    assert meta["status"] == "ok" and meta["problems"] == []
    [seen] = session_output(meta)
    assert seen["refused"]["code"] == 2 and why in seen["refused"]["stderr"], seen["refused"]
    events = gate.after(gate.history(ws), {"event_id": start})
    assert [(e["event"], e["op"]) for e in events] == [("proposed", "patch"), ("proposed", "append"),
                                                       ("proposed", "create")]  # only the default writes


@pytest.mark.parametrize("mode, problem", [
    ("bypass", "stage 模式下未暂存的写入：applied append @overlay/projects/demo/status"),
    ("bypassapply", "不允许的操作：applied move @overlay/projects/demo/status"),
    ("error", "validate 新增 error：error invalid_frontmatter @overlay/broken"),
    ("repofile", "仓库在运行期间变了"),
    ("repocommit", "仓库在运行期间变了"),
])
def test_guardrail_problems_keep_the_checkpoint(cfg, monkeypatch, mode, problem):
    """What gets around the guard (another mindslice on PATH, a file written directly, a change in a watched repo) the
    post-run checks still see."""
    monkeypatch.setenv("FAKE_DREAM", mode)
    meta = runner.run(cfg)
    assert meta["status"] == "guardrail problems" and not meta["advanced"]
    assert any(p.startswith(problem) for p in meta["problems"]), meta["problems"]
    assert "checkpoint" not in gate.load_state(cfg["paths"]["state"])
    assert "checkpoint 未推进" in Path(meta["report"]).read_text()


def test_unstaged_change_to_an_always_loaded_card(cfg, ws, monkeypatch):
    ms(ws, "write", "prefs/p", "--title", "P", "--summary", "A standing preference.", "--content", "- Be brief.",
       "--load", "always", "--no-stage", "--reason", "User asked for it.")
    cfg["dream"]["mode"] = "direct"
    monkeypatch.setenv("FAKE_DREAM", "always")
    meta = runner.run(cfg)
    assert meta["problems"] == ["常驻卡改动未经暂存：applied append @overlay/prefs/p"]


def test_concurrent_write_is_a_warning_and_waits_for_the_next_run(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_DREAM", "concurrent")
    meta = runner.run(cfg)
    assert meta["status"] == "ok" and meta["problems"] == []
    assert meta["warnings"] == ["其他会话同时写入：applied create @overlay/projects/demo/other"
                                "（reason: Another session recorded this.）"]
    t = gate.evaluate(cfg, gate.load_state(cfg["paths"]["state"]), gate.snapshot(cfg))
    assert [w["card"] for w in t["new_writes"]] == ["projects/demo/other"]


@pytest.mark.parametrize("mode, status", [("fail", "harness exit code 3"), ("noreport", "no report")])
def test_failed_session_keeps_the_checkpoint(cfg, monkeypatch, mode, status):
    monkeypatch.setenv("FAKE_DREAM", mode)
    meta = runner.run(cfg)
    assert meta["status"] == status and not meta["advanced"]
    assert "## 脚本检查" in Path(meta["report"]).read_text()
    assert "checkpoint" not in gate.load_state(cfg["paths"]["state"])


def test_timeout_stops_the_session_and_tidies_up(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_DREAM", "sleep")
    cfg["harness"]["timeout_minutes"] = 0.02
    meta = runner.run(cfg)
    assert meta["status"] == "timeout"
    assert gone(session_output(meta)[0]["pid"])
    assert {p.name for p in Path(meta["run_dir"]).iterdir()} == session.RUN_RECORD


def test_processes_left_by_the_session_are_stopped(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_DREAM", "straggler")
    meta = runner.run(cfg)
    straggler = session_output(meta)[0]["straggler"]
    assert gone(straggler), f"process {straggler} still runs"


def test_next_run_tidies_a_run_that_was_killed(cfg):
    old = cfg["paths"]["runs"] / "20261116-030000"
    (old / "work").mkdir(parents=True)
    (old / "work" / "dump.json").write_text("{}")
    (old / "report.md").write_text("# old report\n")
    runner.run(cfg)
    assert [p.name for p in old.iterdir()] == ["report.md"]


def test_report_from_the_final_answer(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_DREAM", "answer")
    meta = runner.run(cfg)
    assert meta["status"] == "ok"
    assert Path(meta["report"]).read_text().startswith("# 来自最终回答的报告")


def test_cli_dry_run_prints_the_task_and_changes_nothing(tmp_path, ws, capsys):
    config = (ROOT / "config.example.toml").read_text().replace('workspace = "~/mindslice"', f'workspace = "{ws}"')
    (tmp_path / "config.toml").write_text(config)
    history = (ws / ".history" / "changes.jsonl").read_text()
    assert cli.main(["--config", str(tmp_path / "config.toml"), "run", "--dry-run"]) == 0
    task = json.loads(capsys.readouterr().out)
    assert task["triggers"] == ["new_writes", "full_sweep"] and task["workspace"] == str(ws)
    assert not (tmp_path / "state.json").exists() and not (tmp_path / "runs").exists()
    assert (ws / ".history" / "changes.jsonl").read_text() == history
