import json

import pytest

from mindslice_dream import guard

STAGE = {"cli": "/x/mindslice", "write": True, "stage": True, "exclude": ["invest/"]}
DREAM = ["--reason", "[dream] checked against git log"]


def refused(argv: list[str], policy: dict = STAGE) -> str:
    with pytest.raises(guard.Refused) as e:
        guard.command(argv, policy)
    return str(e.value)


def test_read_commands_pass_unchanged():
    for argv in (["-w", "/ws", "--json", "read", "a", "b"], ["search", "dream", "--json"], ["diff", "--scope", "overlay"]):
        assert guard.command(argv, STAGE) == argv
        assert guard.command(argv, STAGE | {"write": False}) == argv


def test_stage_mode_stages_every_write():
    argv = ["-w", "/ws", "write", "projects/x", "--op", "patch", "--edit", "a", "b", *DREAM]
    assert guard.command(argv, STAGE) == [*argv, "--stage"]
    assert guard.command([*argv, "--stage"], STAGE) == [*argv, "--stage"]  # not twice
    assert guard.command(argv, STAGE | {"stage": False}) == argv  # direct mode


@pytest.mark.parametrize("argv, why", [
    (["apply"], "不能用 `mindslice apply`"),
    (["-w", "/ws", "bake", "@x"], "不能用 `mindslice bake`"),
    (["write", "projects/x", "--content", "- x", "--no-stage", *DREAM], "--no-stage"),
    (["write", "projects/x", "--content", "- x", "--no-st", *DREAM], "--no-stage"),  # argparse's abbreviation
    (["write", "projects/x", "--op=delete", *DREAM], "不删除卡：projects/x"),
    (["write", "invest/theses/x", "--content", "- x", *DREAM], "invest/theses/x 由别的流程审阅"),
    (["write", "@overlay/invest/macro/x", "--content", "- x", *DREAM], "invest/macro/x 由别的流程审阅"),
    (["write", "projects/x", "--content", "- x"], "reason 要以 `[dream]` 开头"),
    (["write", "projects/x", "--content", "- x", "--reason", "[dream-review] x"], "reason 要以 `[dream]` 开头"),
    (["write", "projects/x", "--content", "- x", "--also", "-", *DREAM], "不能用 `-`"),
])
def test_refused(argv, why):
    assert why in refused(argv)


def test_a_read_only_session_cannot_write():
    assert "只能用读记忆的命令" in refused(["write", "projects/x", "--content", "- x", *DREAM], STAGE | {"write": False})


@pytest.mark.parametrize("members, why", [
    ([{"id": "projects/y", "op": "delete"}], "不删除卡：projects/y"),
    ([{"id": "invest/watchlist", "op": "patch", "edits": []}], "invest/watchlist 由别的流程审阅"),
    ([{"id": "projects/y", "op": "append", "content": "- y", "reason": "another"}], "projects/y：reason"),
    ([{"id": "projects/y", "op": "append", "content": "- y", "reason": None}], "projects/y：reason"),
    ({"id": "projects/y"}, "要是 JSON 列表"),
])
def test_also_members_are_checked(tmp_path, members, why):
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps(members))
    assert why in refused(["write", "projects/x", "--op", "append", "--content", "- x", "--also", str(batch), *DREAM])


def test_also_members_take_the_calls_reason(tmp_path):
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps([{"id": "projects/y", "op": "append", "content": "- y"}]))
    argv = ["write", "projects/x", "--op", "append", "--content", "- x", "--also", str(batch), *DREAM]
    assert guard.command(argv, STAGE) == [*argv, "--stage"]
    batch.write_text("not json")
    assert "读不了 `--also` 文件" in refused(argv)


def test_main_reports_a_refusal(capsys):
    assert guard.main(["discard"], STAGE) == 2
    assert capsys.readouterr().err.startswith("mindslice-dream 拒绝了这次调用，它没有执行：")
