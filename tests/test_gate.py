from datetime import date, timedelta

from conftest import commit, ms
from mindslice_dream import gate, runner


def task(cfg: dict, force: bool = False) -> dict:
    return gate.evaluate(cfg, gate.load_state(cfg["paths"]["state"]), gate.snapshot(cfg), force)


def settle(cfg: dict) -> None:
    """One successful run, so the state holds a checkpoint and today's full sweep."""
    assert runner.run(cfg)["status"] == "ok"


def test_first_run_takes_recent_writes_and_a_full_sweep(cfg):
    t = task(cfg)
    assert t["triggers"] == ["new_writes", "full_sweep"]
    assert [w["card"] for w in t["new_writes"]] == ["projects/demo/status"]
    assert t["new_writes"][0]["writes"][0]["reason"] == "User asked to record the demo state."


def test_idle_night_is_skipped(cfg):
    settle(cfg)
    runs = sorted(cfg["paths"]["runs"].iterdir())
    assert runner.run(cfg) == {"status": "skipped"}
    assert sorted(cfg["paths"]["runs"].iterdir()) == runs
    assert gate.load_state(cfg["paths"]["state"])["last_run"]["status"] == "skipped"


def test_other_sessions_writes_trigger_grouped_per_card(cfg, ws):
    settle(cfg)
    ms(ws, "write", "projects/demo/other", "--title", "Other", "--summary", "Another card.", "--content", "- One.",
       "--reason", "first")
    ms(ws, "write", "projects/demo/other", "--op", "append", "--content", "- Two.", "--reason", "second")
    t = task(cfg)
    assert t["triggers"] == ["new_writes"]
    assert [(w["card"], [x["reason"] for x in w["writes"]]) for w in t["new_writes"]] == [
        ("projects/demo/other", ["first", "second"])]


def test_dream_and_invest_writes_do_not_trigger(cfg, ws):
    settle(cfg)
    ms(ws, "write", "projects/demo/other", "--title", "O", "--summary", "O.", "--content", "- o", "--reason", "[dream] x")
    ms(ws, "write", "projects/demo/other", "--op", "append", "--content", "- p",
       "--reason", "[dream-review] 用户回复审阅邮件：改一下")  # the email review's patch for the user
    ms(ws, "write", "invest/theses/x", "--title", "X", "--summary", "X.", "--content", "- x", "--tag", "invest",
       "--reason", "news-agent run")
    t = task(cfg)
    assert t["triggers"] == [] and t["new_writes"] == []


def test_new_validate_issue_triggers_but_known_ones_do_not(cfg, ws):
    ms(ws, "write", "projects/demo/hub", "--title", "Hub", "--summary", "Links.", "--content", "- See [[projects/gone]].",
       "--reason", "setup")
    settle(cfg)  # the broken link existed before and after that run
    assert task(cfg)["triggers"] == []
    (ws / "overlay" / "broken.md").write_text("+++\ntitle = \n+++\n")  # an outside edit, no history event
    t = task(cfg)
    assert t["triggers"] == ["validate"]
    assert "invalid_frontmatter" in [i["code"] for i in t["validate_new"]]


def test_changed_repo_head_triggers(cfg, repo):
    settle(cfg)
    commit(repo, "second")
    t = task(cfg)
    assert t["triggers"] == ["repo_head"]
    [r] = t["repos"]
    assert r["changed"] and r["previous"] != r["head"]


def test_full_sweep_is_due_after_seven_days(cfg):
    settle(cfg)
    state = gate.load_state(cfg["paths"]["state"])
    assert state["last_full_sweep"] == date.today().isoformat()
    state["last_full_sweep"] = (date.today() - timedelta(days=7)).isoformat()
    gate.save_state(cfg["paths"]["state"], state)
    assert task(cfg)["triggers"] == ["full_sweep"]


def test_force(cfg):
    settle(cfg)
    assert task(cfg, force=True)["triggers"] == ["force"]


def test_oversized_cards_are_listed_on_full_sweeps_and_when_newly_written(cfg, ws):
    cfg["dream"]["max_card_chars"] = 300
    log = "\n".join(f"- Release {i}: tagged, published and installed." for i in range(10))
    ms(ws, "write", "projects/demo/log", "--title", "Log", "--summary", "Releases.", "--content", log, "--reason", "made")
    ms(ws, "write", "invest/theses/long", "--title", "L", "--summary", "L.", "--content", log, "--tag", "invest",
       "--reason", "news-agent run")
    ms(ws, "bake", "@demo", "--prefix", "projects/")  # the slice copy and the @overlay copy share one path
    ms(ws, "write", "projects/demo/log", "--op", "append", "--content", "- Release 10.", "--reason", "more")
    t = task(cfg)  # first run: a full sweep
    assert t["oversized"] == [{"card": "projects/demo/log", "chars": len("Releases.\n\n" + log + "\n- Release 10."),
                               "bullets": 11}]
    settle(cfg)
    assert task(cfg)["oversized"] == []  # no full sweep and not written since
    ms(ws, "write", "projects/demo/log", "--op", "append", "--content", "- Release 11.", "--reason", "again")
    assert [c["card"] for c in task(cfg)["oversized"]] == ["projects/demo/log"]


def test_deleted_cards_are_not_listed(cfg, ws):
    settle(cfg)
    ms(ws, "write", "projects/demo/other", "--title", "O", "--summary", "O.", "--content", "- o", "--reason", "made")
    ms(ws, "write", "projects/demo/other", "--op", "delete", "--reason", "not needed after all")
    assert task(cfg)["new_writes"] == []
