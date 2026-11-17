import json
import sys
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

import pytest

from conftest import ROOT, ms
from fake_mail import SENT, FakeIMAP, FakeSMTP, Mailbox
from mindslice_dream import gate, mail, review, runner, session


@pytest.fixture
def box(monkeypatch, cfg, tmp_path) -> Mailbox:
    """Mail on, through a fake Gmail: reviews go to the account's plus address, never to news-agent's list."""
    box = Mailbox()
    monkeypatch.setattr(FakeSMTP, "box", box, raising=False)
    monkeypatch.setattr(FakeIMAP, "box", box, raising=False)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    account = tmp_path / "smtp.toml"
    account.write_text('host = "smtp.example.com"\nport = 465\nusername = "me@example.com"\n'
                       'password = "app-password"\nto = ["me@example.com", "friend@example.com"]\n')
    cfg["mail"] = {"to": "me+dream@example.com", "account": account, "imap": "imap.example.com", "timeout_minutes": 1}
    return box


def text(msg: EmailMessage) -> str:
    return msg.get_body(("plain",)).get_content().replace("\r\n", "\n")  # as delivered: CRLF


def reply(box: Mailbox, to: EmailMessage, words: str, sender: str = "Me <me@example.com>", labels=SENT,
          thread: bool = True, recipient: str = "me+dream@example.com") -> EmailMessage:
    """The user's answer to a mail, the way Gmail writes it: their words, then the quoted mail."""
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f"Re: {to['Subject']}", sender, recipient
    msg["Message-ID"] = make_msgid(domain="mail.example.com")
    if thread:
        msg["In-Reply-To"] = msg["References"] = str(to["Message-ID"])
    quoted = "\n".join(f"> {line}" for line in text(to).splitlines())
    msg.set_content(f"{words}\n\nOn Wed, Nov 18, 2026 at 3:15 AM <me+dream@example.com> wrote:\n\n{quoted}\n")
    box.deliver(msg, labels)
    return msg


def reviewer(cfg: dict, monkeypatch, *decisions: dict, mode: str = "") -> None:
    """The next sessions read replies with the fake reviewer, which answers with these decisions."""
    cfg["harness"]["cmd"] = [sys.executable, str(ROOT / "tests" / "fake_reviewer.py"), "{prompt}"]
    monkeypatch.setenv("FAKE_DECISIONS", json.dumps({"decisions": list(decisions), "message": ""}, ensure_ascii=False))
    monkeypatch.setenv("FAKE_REVIEW", mode)


def staged(ws: Path) -> list[str]:
    return [it["id"] for it in json.loads(ms(ws, "--json", "diff"))["items"]]


def seen(cfg: dict) -> dict:
    """What the last reviewing session was given."""
    run_dir = sorted(cfg["paths"]["runs"].glob("*-review"))[-1]
    return json.loads((run_dir / "stdout.log").read_text())["seen"]


def test_run_mails_the_dream_changes(cfg, box):
    meta = runner.run(cfg)
    [msg] = box.received()
    assert meta["mail"] == f"sent: {msg['Subject']}"
    assert msg["Subject"] == f"[MindSlice dream {meta['run_id']}] 2 处改动待审"
    assert (msg["From"], msg["To"]) == ("me@example.com", "me+dream@example.com")
    assert str(msg["Message-ID"]).endswith("@mindslice-dream>")  # not this machine's hostname
    body = text(msg)
    assert "━━ 1. projects/demo/notes（新卡）\nreason：[dream] fake: checked against git log" in body
    assert "━━ 2. projects/demo/status（改 @overlay 现行版本）" in body
    assert ("-- State: the fix is uncommitted.\n+- State: the fix is committed in abc1234.\n"
            "+- Verified 2026-11-18 against git log") in body
    assert "━━ 运行报告\n\n# Dream 报告" in body and "## 脚本检查" in body
    assert '<span style="background:#ffebe9">-- State: the fix is uncommitted.</span>' in msg.get_body(("html",)).get_content()
    assert "friend@example.com" not in msg.as_string()
    rec = gate.load_state(cfg["paths"]["state"])["mail"]["review"]
    assert rec["message_id"] == str(msg["Message-ID"]) and rec["run_id"] == meta["run_id"]
    assert [(i["n"], i["id"], i["state"]) for i in rec["items"]] == [(1, "projects/demo/notes", "pending"),
                                                                    (2, "projects/demo/status", "pending")]


def test_nothing_staged_and_an_ok_run_sends_nothing(cfg, box):
    cfg["dream"]["mode"] = "direct"
    assert runner.run(cfg)["mail"] == "not needed" and box.messages == []


def test_a_failed_run_is_mailed(cfg, box, monkeypatch):
    monkeypatch.setenv("FAKE_DREAM", "fail")
    meta = runner.run(cfg)
    [msg] = box.received()
    assert msg["Subject"] == f"[MindSlice dream {meta['run_id']}] harness exit code 3"
    assert "运行没有成功（harness exit code 3），checkpoint 没有推进" in text(msg)
    assert "review" not in gate.load_state(cfg["paths"]["state"]).get("mail", {})


def test_the_same_pending_changes_are_mailed_once_unless_asked(cfg, box):
    runner.run(cfg)
    first = box.received()[0]["Message-ID"]
    with session.locked(cfg):
        assert review.notify(cfg, "20261119-030000", "ok", None) == "not needed"
    assert review.send_now(cfg).startswith("sent: ")
    assert len(box.messages) == 2
    assert gate.load_state(cfg["paths"]["state"])["mail"]["superseded"] == [str(first)]


def test_a_reply_is_carried_out_and_confirmed(cfg, ws, box, monkeypatch):
    runner.run(cfg)
    [mailed] = box.received()
    answer = reply(box, mailed, "1 放弃。\n2：把 committed in abc1234 改成 committed and pushed in abc1234 再批准\n\n-- \nMe")
    reviewer(cfg, monkeypatch, {"n": 1, "action": "discard", "quote": "1 放弃"},
             {"n": 2, "action": "modify", "quote": "2：把 committed in abc1234 改成 committed and pushed in abc1234 再批准",
              "edits": [{"old": "committed in abc1234", "new": "committed and pushed in abc1234"}]})
    assert review.poll(cfg) == ["处理了一封回复：修改后生效 1，已放弃 1"]

    # the session got the user's words only, and the staged text it patches
    task = seen(cfg)
    assert task["reply"] == "1 放弃。\n2：把 committed in abc1234 改成 committed and pushed in abc1234 再批准"
    assert [(i["n"], i["state"]) for i in task["items"]] == [(1, "pending"), (2, "pending")]
    assert "- State: the fix is committed in abc1234." in task["items"][1]["staged"]

    assert staged(ws) == []
    card = json.loads(ms(ws, "--json", "read", "projects/demo/status"))
    assert "committed and pushed in abc1234" in card["body"] and "Verified 2026-11-18" in card["body"]
    patch = [e for e in gate.history(ws) if e.get("op") == "patch"][-1]
    assert patch["reason"].startswith("[dream-review] 用户回复审阅邮件：2：把 committed in abc1234")

    confirm = box.received()[-1]
    assert confirm["Subject"] == f"Re: {mailed['Subject']}"
    assert confirm["In-Reply-To"] == answer["Message-ID"] and str(mailed["Message-ID"]) in confirm["References"]
    body = text(confirm)
    assert "修改后生效：#2 projects/demo/status，最终文本：" in body and "committed and pushed in abc1234" in body
    assert "已放弃：#1 projects/demo/notes" in body and "这封审阅邮件里的改动都处理完了。" in body
    assert [i["state"] for i in gate.load_state(cfg["paths"]["state"])["mail"]["review"]["items"]] == [
        "discarded", "modified"]
    logins = box.logins
    assert review.poll(cfg) == [] and box.logins == logins  # nothing left to review: no IMAP connection


def test_a_reply_is_carried_out_once(cfg, ws, box, monkeypatch):
    runner.run(cfg)
    reply(box, box.received()[0], "1 批准，2 先放着")
    reviewer(cfg, monkeypatch, {"n": 1, "action": "approve"}, {"n": 2, "action": "pending", "note": "用户说先放着"})
    assert review.poll(cfg) == ["处理了一封回复：已生效 1，保持待审 1"]
    assert staged(ws) == ["projects/demo/status"]
    body = text(box.received()[-1])
    assert "已生效：#1 projects/demo/notes" in body and "保持待审：\n- #2 projects/demo/status：用户说先放着" in body
    assert "还有 1 处待审：#2。直接回复这封邮件继续。" in body
    count = len(box.messages)
    assert review.poll(cfg) == [] and len(box.messages) == count


@pytest.mark.parametrize("trap", ["not sent from the account", "another sender", "another thread", "a forward"])
def test_replies_that_do_not_count_are_ignored(cfg, ws, box, monkeypatch, trap):
    runner.run(cfg)
    mailed = box.received()[0]
    if trap == "not sent from the account":  # anyone can write that From header; only the owner gets \Sent
        reply(box, mailed, "全部批准", labels=(b'"\\\\Inbox"',))
    elif trap == "another sender":
        reply(box, mailed, "全部批准", sender="Mallory <mallory@example.com>")
    elif trap == "another thread":
        reply(box, mailed, "全部批准", thread=False)
    else:
        reply(box, mailed, "全部批准", recipient="Friend <friend@example.com>")
    reviewer(cfg, monkeypatch, {"n": 1, "action": "approve"}, {"n": 2, "action": "approve"})
    assert review.poll(cfg) == []
    assert staged(ws) == ["projects/demo/notes", "projects/demo/status"] and len(box.messages) == 2


def test_a_change_staged_again_after_the_mail_is_not_executed(cfg, ws, box, monkeypatch):
    runner.run(cfg)
    ms(ws, "write", "projects/demo/status", "--op", "append", "--content", "- Later.", "--stage",
       "--reason", "[dream] a later run")
    reply(box, box.received()[0], "2 批准")
    reviewer(cfg, monkeypatch, {"n": 2, "action": "approve"})
    review.poll(cfg)
    assert staged(ws) == ["projects/demo/notes", "projects/demo/status"]
    assert "没执行：\n- #2 projects/demo/status：发信后暂存版本变过" in text(box.received()[-1])


@pytest.mark.parametrize("mode, note", [("write", "解读回复期间记忆库里出现了 1 条写入，为安全起见这次什么都没执行"),
                                        ("fail", "没能读懂这封回复（harness exit code 3），这次什么都没执行")])
def test_nothing_is_executed_when_the_reading_goes_wrong(cfg, ws, box, monkeypatch, mode, note):
    runner.run(cfg)
    reply(box, box.received()[0], "全部批准")
    reviewer(cfg, monkeypatch, {"n": 1, "action": "approve"}, {"n": 2, "action": "approve"}, mode=mode)
    review.poll(cfg)
    assert {"projects/demo/notes", "projects/demo/status"} <= set(staged(ws))
    assert note in text(box.received()[-1])


def test_a_reply_to_a_superseded_mail_gets_a_note(cfg, ws, box, monkeypatch):
    runner.run(cfg)
    old = box.received()[0]
    ms(ws, "write", "projects/demo/status", "--op", "append", "--content", "- Later.", "--stage",
       "--reason", "[dream] a later run")
    review.send_now(cfg)
    reply(box, old, "全部批准")
    reviewer(cfg, monkeypatch, {"n": 1, "action": "approve"}, {"n": 2, "action": "approve"})
    assert review.poll(cfg) == ["回复的是被取代的审阅邮件，已请用户回复最新那封"]
    assert len(staged(ws)) == 2
    assert "已经被更新的一封取代" in text(box.received()[-1])


def test_a_card_is_not_applied_without_the_new_card_it_extends(cfg, ws, box, monkeypatch):
    ms(ws, "write", "projects/demo/base", "--title", "Base", "--summary", "A new base card.", "--content", "- b",
       "--stage", "--reason", "[dream] split out")
    ms(ws, "write", "projects/demo/child", "--title", "Child", "--summary", "Refines the base.", "--content", "- c",
       "--extends", "@overlay/projects/demo/base", "--stage", "--reason", "[dream] split out")
    assert review.send_now(cfg).startswith("sent: ")
    assert "#2 依赖 #1：要一起批准" in text(box.received()[0])
    reply(box, box.received()[0], "2 批准")
    reviewer(cfg, monkeypatch, {"n": 2, "action": "approve"})
    review.poll(cfg)
    assert staged(ws) == ["projects/demo/base", "projects/demo/child"]
    assert "- #2 projects/demo/child：没生效：它引用的 @overlay/projects/demo/base（#1）还没生效，要一起批准" in text(
        box.received()[-1])


def test_the_diff_leaves_out_what_the_engine_adds_to_a_copied_card(cfg, ws):
    ms(ws, "write", "projects/demo/hub", "--title", "Hub", "--summary", "Links to the status.",
       "--content", "- See [[projects/demo/status]].\n- Old line.", "--reason", "setup")
    ms(ws, "bake", "@demo", "--prefix", "projects/", "--name", "demo", "--description", "Demo slice.", "--yes")
    ms(ws, "write", "projects/demo/hub", "--op", "patch", "--edit", "Old line.", "New line.", "--stage", "--reason", "[dream] x")
    [item], others = review.staged_items(cfg)
    # the copy's supersedes pointer and its [[@demo/...]] link are the engine's, not the dream's
    assert (item["source"], item["diff"], item["folded"], others) == ("从 @demo 复制并修改", ["-- Old line.", "+- New line."], 1, 0)
    ms(ws, "apply", "projects/demo/hub")
    ms(ws, "write", "projects/demo/hub", "--op", "patch", "--edit", "New line.", "Newer line.", "--stage", "--reason", "[dream] y")
    [item], _ = review.staged_items(cfg)
    assert (item["source"], item["diff"], item["folded"]) == ("改 @overlay 现行版本", ["-- New line.", "+- Newer line."], 0)


def test_poll_waits_for_a_review_and_for_the_dream(cfg, box):
    assert review.poll(cfg) == [] and box.logins == 0  # no review mail yet: no IMAP connection
    runner.run(cfg)
    with session.locked(cfg):
        assert review.poll(cfg) == ["dream 正在运行，这一轮跳过"]
    assert box.logins == 0


def test_mail_is_off_without_an_address(cfg):
    assert cfg["mail"]["to"] == "" and runner.run(cfg)["mail"] == "off"


def test_reply_text_keeps_only_the_users_words():
    msg = EmailMessage()
    msg.set_content("全部批准，除了 3。\n3 放弃\n\nMe <me+dream@example.com> 于2026年11月18日周三 03:15写道：\n\n"
                    "> ━━ 1. projects/a\n> -old\n\n-- \nMe, me@example.com\n")
    assert mail.reply_text(msg) == "全部批准，除了 3。\n3 放弃"
    html_only = EmailMessage()
    html_only.set_content('<div>1 批准<br>写给 me@example.com</div><div class="gmail_quote">> quoted</div>', subtype="html")
    assert mail.reply_text(html_only) == "1 批准\n写给 <邮箱>"


def test_gmail_labels_are_unquoted():
    prefix = b'12 (X-GM-LABELS (\\Inbox "\\\\Sent" "a (b)") UID 5 BODY[HEADER] {3}'
    assert mail.labels(prefix) == {"\\Inbox", "\\Sent", "a (b)"}
    assert mail.mailbox("Me <Me+dream@Example.com>") == "me@example.com"
