"""The only `mindslice` a harness session gets (PLAN section 7): rules with one right answer are kept by the program,
not by the prompt. Read commands pass; `write` passes only in the dream session, staged in stage mode, never a delete
or --no-stage, with a `[dream]` reason, outside the excluded cards and with --if-match on a replace, --also members
included. What passes runs the
real CLI unchanged, plus --stage. The post-run checks stay as a backstop for what this cannot see (another CLI, files)."""

import json
import os
import sys
from pathlib import Path

from mindslice.interfaces.cli import build_parser  # the engine's own parser: options read exactly as the CLI reads them

from .gate import card_path

READ_COMMANDS = {"read", "search", "manifest", "validate", "diff", "history", "status", "explain"}


class Refused(Exception):
    pass


def command(argv: list[str], policy: dict) -> list[str]:
    """The arguments to run the real CLI with; Refused says why the call is not allowed."""
    args = build_parser().parse_args(argv)  # a usage error or --help exits here, as the CLI itself would
    if args.command in READ_COMMANDS:
        return argv
    if args.command != "write" or not policy["write"]:
        allowed = "读记忆的命令和 `write`" if policy["write"] else "读记忆的命令"
        raise Refused(f"这个会话不能用 `mindslice {args.command}`，只能用{allowed}")
    if args.stage is False:
        raise Refused("不能用 `--no-stage`")
    writes = [{"id": args.id, "op": args.op, "reason": args.reason, "if_match": args.if_match}]
    if args.also == "-":
        raise Refused("`--also` 要给 JSON 文件，不能用 `-`")
    if args.also:
        try:
            also = json.loads(Path(args.also).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise Refused(f"读不了 `--also` 文件：{e}") from None
        if not isinstance(also, list) or not all(isinstance(w, dict) for w in also):
            raise Refused("`--also` 文件要是 JSON 列表，每项是一次写入的对象")
        writes += [{"reason": args.reason, **w} for w in also]  # a member without a reason takes the call's
    for w in writes:
        path = card_path(str(w.get("id")))
        if w.get("op", "create") == "delete":
            raise Refused(f"dream 不删除卡：{path}")
        if path.startswith(tuple(policy["exclude"])):
            raise Refused(f"{path} 由别的流程审阅，dream 不写")
        if not str(w.get("reason") or "").startswith("[dream]"):
            raise Refused(f"{path}：reason 要以 `[dream]` 开头，后面写证据来源")
        if w.get("op") == "replace" and not w.get("if_match"):
            raise Refused(f"{path}：replace 要带读卡时拿到的 sha256（`--if-match`，`--also` 成员写 `if_match`）")
    return [*argv, "--stage"] if policy["stage"] and args.stage is None else argv


def main(argv: list[str], policy: dict) -> int:
    try:
        args = command(argv, policy)
    except Refused as e:
        print(f"mindslice-dream 拒绝了这次调用，它没有执行：{e}", file=sys.stderr)
        return 2
    os.execv(policy["cli"], [policy["cli"], *args])
