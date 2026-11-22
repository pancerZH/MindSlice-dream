import json
from pathlib import Path

import pytest

from conftest import ms
from mindslice_dream import drops, gate, runner

WHY = "User asked to record it."


@pytest.fixture
def baked(ws) -> Path:
    """A slice @demo holding an old card that two cards link to; @overlay holds a copy of the old card and a copy of
    one linking card that no longer links to it."""
    for card, content in [("old", "- Done long ago."), ("topic", "- See [[projects/demo/old]]."),
                          ("other", "- Also [[projects/demo/old]].")]:
        ms(ws, "write", f"projects/demo/{card}", "--title", card.title(), "--summary", f"The {card} card.",
           "--content", content, "--reason", WHY)
    ms(ws, "bake", "@demo", "--prefix", "")
    ms(ws, "write", "projects/demo/topic", "--op", "patch", "--edit", "See [[projects/demo/old]].", "Nothing left.",
       "--reason", WHY)
    ms(ws, "write", "projects/demo/old", "--op", "append", "--content", "- A copy.", "--reason", WHY)
    return ws


def proposal(card: str, **extra) -> dict:
    return {"card": card, "reason": "只剩历史。", "evidence": ["commit abc1234"], "migrate": []} | extra


def test_proposals_are_checked_and_listed_with_what_dropping_touches(cfg, baked, monkeypatch):
    ms(baked, "write", "projects/demo/ext", "--title", "Ext", "--summary", "Refines the old card.", "--content", "- More.",
       "--extends", "@demo/projects/demo/old", "--reason", WHY)
    monkeypatch.setenv("FAKE_DROPS", json.dumps([
        proposal("@demo/projects/demo/old", migrate=[{"text": "Done long ago.", "to": "projects/demo/topic"}]),
        proposal("projects/demo/other", evidence=[]),
        proposal("invest/theses/x"),
        proposal("projects/demo/missing"),
    ]))
    meta = runner.run(cfg)
    assert meta["status"] == "ok"
    assert (Path(meta["run_dir"]) / drops.FILE).exists()  # part of the run record

    [pending] = gate.load_state(cfg["paths"]["state"])["drops"]["pending"]
    assert pending == proposal("projects/demo/old", run=Path(meta["run_dir"]).name,
                               migrate=[{"text": "Done long ago.", "to": "projects/demo/topic"}])
    report = Path(meta["report"]).read_text()
    section = report[report.index("## 可以整张删掉的卡（程序整理）"):report.index("## 脚本检查")]
    assert f"- `projects/demo/old`（@demo；{pending['run']} 提出）" in section
    assert "删前挪走：「Done long ago.」→ `projects/demo/topic`" in section
    # the slice's topic card still links to it, but the @overlay copy that replaced it does not
    assert "还链接它的卡：`@demo/projects/demo/other`\n" in section
    assert "扩展它的卡（不先处理，bake --drop 会拒绝）：`@overlay/projects/demo/ext`" in section
    assert "@overlay 里有它的副本" in section
    assert meta["drops_refused"] == ["`projects/demo/other`：evidence 至少要有一条依据",
                                     "`invest/theses/x`：这张卡由别的流程管", "`projects/demo/missing`：没有这张卡"]
    assert all(r in section for r in meta["drops_refused"])


def test_open_proposals_stay_until_the_card_is_dropped(cfg, baked, monkeypatch):
    monkeypatch.setenv("FAKE_DROPS", json.dumps([proposal("projects/demo/old")]))
    runner.run(cfg)
    assert "`projects/demo/old`" in drops.show(cfg)  # a later look still has it

    ms(baked, "write", "projects/demo/old", "--op", "delete", "--reason", WHY)  # the @overlay copy first
    ms(baked, "bake", "@demo", "--drop", "projects/demo/old")
    assert drops.show(cfg) == "没有待处理的整卡删除建议。"
    assert gate.load_state(cfg["paths"]["state"])["drops"]["pending"] == []


def test_a_kept_card_is_not_proposed_again(cfg, baked, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_DROPS", json.dumps([proposal("projects/demo/old")]))
    runner.run(cfg)
    assert drops.show(cfg, keep="projects/demo/nope", why="x") == "没有这张卡：projects/demo/nope"
    assert drops.show(cfg, keep="@demo/projects/demo/old", why="它是 topic 设计的依据") == "没有待处理的整卡删除建议。"

    state = gate.load_state(cfg["paths"]["state"])
    assert state["drops"]["pending"] == []
    assert [(k["card"], k["why"]) for k in state["drops"]["kept"]] == [("projects/demo/old", "它是 topic 设计的依据")]
    assert gate.evaluate(cfg, state, gate.snapshot(cfg))["drops"]["kept"] == state["drops"]["kept"]
    (tmp_path / drops.FILE).write_text(json.dumps([proposal("projects/demo/old")]))
    assert drops.proposals(tmp_path / drops.FILE, state["drops"], ()) == ([], ["`projects/demo/old`：用户决定保留这张卡"])


def test_a_card_proposed_again_takes_the_new_proposal():
    old = {"pending": [proposal("a") | {"run": "r1"}, proposal("b") | {"run": "r1"}], "kept": []}
    new = drops.merge(old, [proposal("b", reason="内容已挪走。")], "r2")
    assert new["pending"] == [proposal("a") | {"run": "r1"}, proposal("b", reason="内容已挪走。") | {"run": "r2"}]


def test_a_malformed_file_is_refused_whole(tmp_path):
    (tmp_path / drops.FILE).write_text("[{")
    found, refused = drops.proposals(tmp_path / drops.FILE, {}, ())
    assert found == [] and refused[0].startswith(f"{drops.FILE} 不是合法的 JSON")
