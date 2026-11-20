"""Review by email (PLAN section 10): mail the dream's staged changes, read the user's reply, carry it out.

One harness session reads the reply and only answers with decisions; the program checks them, executes them through
the engine's workspace lock and revision-checked review and confirms in the same mail thread."""

import difflib
import html
import json
import re
import tomllib
from collections import Counter
from datetime import datetime, timezone
from email import message_from_string, policy
from pathlib import Path

from mindslice import MindSliceError, Workspace
from mindslice.operations.writer import fork_seed

from . import gate, mail, session

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "review.md"
REVIEW_PREFIX = "[dream-review]"
FIELDS = ("title", "aliases", "tags", "extends", "supersedes", "load")
OUTCOMES = {"applied": "已生效", "modified": "修改后生效", "discarded": "已放弃", "pending": "保持待审", "skipped": "没执行"}
HANDLED = ("applied", "modified", "discarded")

HOWTO = """直接回复这封邮件，写明编号和怎么处理，例如：
  全部批准
  批准 1-5，放弃 6
  3：把"旧的说法"改成"新的说法"再批准
没提到的保持待审。修改后直接生效，确认邮件里会给出最终文本。"""
SUPERSEDED = "这封审阅邮件已经被更新的一封取代：{subject}。这次回复里的决定没有执行，请回复最新那封。"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- what is staged


def parse_card(text: str) -> tuple[dict, list[str]]:
    """A card file: its TOML front matter, and its summary and body lines."""
    _, front, body = text.split("+++", 2)
    return tomllib.loads(front), body.strip().splitlines()


def mount_dirs(ws: Path) -> dict[str, Path]:
    mounts = tomllib.loads((ws / "mounts.toml").read_text(encoding="utf-8")).get("mounts", [])
    return {m["alias"]: (ws / m["path"]).resolve() for m in mounts}


def card_diff(ws: Path, item: dict, catalogs) -> tuple[str, list[str], int]:
    """How a staged card differs from the live one: (where it comes from, changed lines, lines whose only change is
    a link the engine qualified with a slice prefix when it copied the card into @overlay)."""
    rel = item["id"]
    try:
        meta, new = parse_card((ws / "staging" / f"{rel}.md").read_text(encoding="utf-8"))
        live = ws / "overlay" / f"{rel}.md"
        # not yet in @overlay: a copy of a read-only slice's card, pointing at it through supersedes
        origin = None if live.exists() else next((a for a in meta.get("supersedes", []) if gate.card_path(a) == rel), None)
        if live.exists():
            source, (base, old) = "改 @overlay 现行版本", parse_card(live.read_text(encoding="utf-8"))
        elif origin:
            alias = origin.split("/", 1)[0]
            source = f"从 {alias} 复制并修改"
            raw = (mount_dirs(ws)[alias] / f"{rel}.md").read_text(encoding="utf-8")
            base, old = parse_card(raw)
            _, qualified = parse_card(fork_seed(raw, origin, catalogs))
        else:
            source, base, old = "新卡", {}, []
    except (OSError, KeyError, ValueError):  # a staged delete, a slice mounted from an archive, ...
        return item["change"], item["diff"].splitlines(), 0
    lines = []
    for k in FIELDS:
        was, now = base.get(k), meta.get(k)
        if k == "supersedes" and origin:
            now = [a for a in now if a != origin]  # the copy's own pointer to the card it replaces
        if (was or None) != (now or None):
            lines += [f"- {k}: {json.dumps(was, ensure_ascii=False)}"] if was else []
            lines += [f"+ {k}: {json.dumps(now, ensure_ascii=False)}"] if now else []
    folded = 0
    matcher = difflib.SequenceMatcher(a=qualified if origin else old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            folded += sum(a != b for a, b in zip(old[i1:i2], new[j1:j2]))
        else:
            lines += [f"-{x}" for x in old[i1:i2]] + [f"+{x}" for x in new[j1:j2]]
    return source, lines, folded


def staged_items(cfg: dict) -> tuple[list[dict], int]:
    """The dream's pending changes in staging, one per card, and the number of other pending changes."""
    ws = cfg["memory"]["workspace"]
    with Workspace(ws) as engine, engine._reading():
        return _staged_items(ws, engine.diff()["items"], engine.index.catalogs())


def _staged_items(ws, pending, catalogs):
    items = []
    for it in pending:
        reasons = [c.get("reason") or "" for c in it.get("changes", [])]
        if not reasons or not all(r.startswith(gate.DREAM_PREFIXES) for r in reasons):
            continue  # staged by another flow (news-agent's invest/*, a session's change to an always-loaded card)
        source, diff, folded = card_diff(ws, it, catalogs)
        refs = it.get("references", {})
        items.append({"id": it["id"], "address": it["address"], "sha256": it["staged_sha256"],
                      "change_ids": it.get("change_ids", []), "text": staged_text(ws, it["id"]),
                      "create": it["change"] == "create", "source": source, "reasons": list(dict.fromkeys(reasons)),
                      "diff": diff, "folded": folded, "refs": refs.get("extends", []) + refs.get("supersedes", [])})
    return items, len(pending) - len(items)


def staged_text(ws: Path, card: str) -> str:
    """The staged version's summary and body: what a modification patches ("" for a staged delete)."""
    try:
        return "\n".join(parse_card((ws / "staging" / f"{card}.md").read_text(encoding="utf-8"))[1])
    except (OSError, ValueError):
        return ""


def dependencies(items: list[dict]) -> dict[int, list[int]]:
    """#n -> the items whose new card #n refers to: applied without them, its extends / supersedes would dangle."""
    new = {it["address"]: n for n, it in enumerate(items, 1) if it["create"]}
    deps = {n: [new[a] for a in it["refs"] if a in new and new[a] != n] for n, it in enumerate(items, 1)}
    return {n: d for n, d in deps.items() if d}


# ---------------------------------------------------------------- the review mail


def as_html(blocks: list[tuple[str, bool]]) -> str:
    """The mail as one preformatted page; in the diffs, removed and added lines are highlighted."""
    lines = []
    for text, is_diff in blocks:
        for line in text.splitlines():
            shown = html.escape(line)
            if is_diff and line[:1] in ("-", "+"):
                shown = f'<span style="background:{"#ffebe9" if line[0] == "-" else "#e6ffec"}">{shown}</span>'
            lines.append(shown)
        lines.append("")
    body = "\n".join(lines)
    return ('<pre style="white-space:pre-wrap;word-break:break-word;'
            f'font:13px/1.5 ui-monospace,Menlo,Consolas,monospace">{body}</pre>')


def review_mail(run_id: str, status: str, items: list[dict], others: int, report: str) -> tuple[str, str, str]:
    """Subject, text and HTML of the mail that lists the pending changes, or reports a failed run."""
    subject = f"[{mail.SUBJECT_TAG} {run_id}] " + (f"{len(items)} 处改动待审" if items else status)
    if items and status != "ok":
        subject += f" · {status}"
    head = [f"MindSlice dream {run_id}：" + ("运行正常。" if status == "ok"
                                             else f"运行没有成功（{status}），checkpoint 没有推进，下次重来。")]
    if items:
        head += [f"staging 里有 {len(items)} 处 dream 的改动等你审阅（包括以前没处理完的），这封邮件取代之前的审阅邮件。",
                 HOWTO]
        head += [f"#{n} 依赖 {'、'.join(f'#{d}' for d in deps)}：要一起批准，只批准 #{n} 的话它会保持待审。"
                 for n, deps in dependencies(items).items()]
    if others:
        head.append(f"另有 {others} 处暂存改动不是 dream 的，不在这封邮件里，用 /mindslice:review 审阅。")
    blocks = [("\n\n".join(head), False)]
    for n, it in enumerate(items, 1):
        reasons = "\n".join(f"reason：{r}" for r in it["reasons"])
        blocks += [(f"━━ {n}. {it['id']}（{it['source']}）\n{reasons}", False),
                   ("\n".join(it["diff"]) or "（内容没有变化）", True)]
        if it["folded"]:
            blocks.append((f"另有 {it['folded']} 行只是链接补上了 slice 前缀，文字没变。", False))
    if report:
        blocks.append(("━━ 运行报告\n\n" + report.strip(), False))
    return subject, "\n\n".join(text for text, _ in blocks) + "\n", as_html(blocks)


def notify(cfg: dict, run_id: str, status: str, report: Path | None, force: bool = False) -> str:
    """After a run, with the lock held: mail the dream's pending changes, or the failed run. Returns what happened.

    Not sent when the run is ok and nothing is pending, or (unless forced) when the last review mail listed exactly
    the same staged versions."""
    if not cfg["mail"]["to"]:
        return "off"
    try:  # whatever goes wrong here must not cost the run its record
        items, others = staged_items(cfg)
        state = gate.load_state(cfg["paths"]["state"])
        box = state.setdefault("mail", {})
        current = box.get("review")
        mailed = [(i["id"], i["sha256"], i.get("change_ids")) for i in (current or {}).get("items", []) if i["state"] == "pending"]
        if status == "ok" and (not items or (not force and [(i["id"], i["sha256"], i["change_ids"]) for i in items] == mailed)):
            return "not needed"
        text = report.read_text(encoding="utf-8") if report and report.exists() else ""
        subject, body, page = review_mail(run_id, status, items, others, text)
        msg = mail.compose(cfg, subject, body, page)
        mail.send(cfg, msg)
    except Exception as e:
        return f"failed: {type(e).__name__}: {e}"
    if items:  # a mail without items (a failed run) leaves the current review as it is
        if current:
            box["superseded"] = (box.get("superseded", []) + [current["message_id"]])[-30:]
        box["review"] = {"message_id": str(msg["Message-ID"]), "subject": subject, "run_id": run_id,
                         "sent": session.now_iso(),
                         "items": [{"n": n, "id": it["id"], "sha256": it["sha256"], "change_ids": it["change_ids"],
                                    "state": "pending"}
                                   for n, it in enumerate(items, 1)]}
        gate.save_state(cfg["paths"]["state"], state)
    return f"sent: {subject}"


def send_now(cfg: dict) -> str:
    """`mail --send`: mail the pending dream changes now, even if the last review mail listed the same ones."""
    with session.locked(cfg, wait=False) as free:
        if not free:
            return "dream 正在运行，稍后再试"
        latest = cfg["paths"]["runs"] / "latest.md"
        run_id = latest.resolve().parent.name if latest.exists() else datetime.now().strftime("%Y%m%d-%H%M%S")
        return notify(cfg, run_id, "ok", latest, force=True)


# ---------------------------------------------------------------- the reply


def waiting(cfg: dict) -> bool:
    """Whether the current review mail still lists a change that is staged as it was mailed."""
    review = gate.load_state(cfg["paths"]["state"]).get("mail", {}).get("review")
    if not review:
        return False
    staged = {it["id"]: it["staged_sha256"] for it in gate.mindslice_json(cfg, "diff")["items"]}
    return any(i["state"] == "pending" and staged.get(i["id"]) == i["sha256"] for i in review["items"])


def poll(cfg: dict) -> list[str]:
    """The mail timer: carry out the user's replies to the current review mail; returns log lines. With nothing left
    to review it returns at once, without connecting to the mail server."""
    if not cfg["mail"]["to"]:
        return []
    if "outbox" not in gate.load_state(cfg["paths"]["state"]).get("mail", {}) and not waiting(cfg):
        return []
    with session.locked(cfg, wait=False) as free:
        if not free:
            return ["dream 正在运行，这一轮跳过"]
        state = gate.load_state(cfg["paths"]["state"])
        box = state.get("mail", {})
        if receipt := box.get("outbox"):
            mail.send(cfg, message_from_string(receipt, policy=policy.default))
            del box["outbox"]
            gate.save_state(cfg["paths"]["state"], state)
        if not waiting(cfg):
            return []
        review = box["review"]
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(review["sent"])).days + 2
        lines = []
        for msg in mail.fetch(cfg, days, skip=set(box.get("handled", []))):
            refs = set(mail.ids(msg["References"], msg["In-Reply-To"]))
            if review["message_id"] in refs:
                answer, line = handle(cfg, review, msg)
            elif refs & set(box.get("superseded", [])):
                subject = str(msg["Subject"])
                answer = mail.compose(cfg, subject if subject.lower().startswith("re:") else f"Re: {subject}",
                                      SUPERSEDED.format(subject=review["subject"]), reply_to=msg)
                line = "回复的是被取代的审阅邮件，已请用户回复最新那封"
            else:
                continue  # another thread that matched the search
            box["handled"] = (box.get("handled", []) + [str(msg["Message-ID"]).strip()])[-200:]
            box["outbox"] = answer.as_string()
            gate.save_state(cfg["paths"]["state"], state)  # before the answer goes out: no reply is executed twice
            mail.send(cfg, answer)
            del box["outbox"]
            gate.save_state(cfg["paths"]["state"], state)
            lines.append(line)
        return lines


def decisions(text: str | None) -> dict | None:
    """The {"decisions": [...]} object in the session's final answer, bare or inside a code fence."""
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text or "") if ch == "{"):
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("decisions"), list):
            return value
    return None


def handle(cfg: dict, review: dict, msg) -> tuple:
    """One reply to the current review mail: a session reads it, the program executes, the confirmation is composed.
    The items' states in review follow; the record is kept in runs/<time>-review/."""
    ws = cfg["memory"]["workspace"]
    run_dir = session.new_run_dir(cfg["paths"]["runs"], "-review")
    staged = {it["id"]: it for it in staged_items(cfg)[0]}
    items = []
    for i in review["items"]:
        it = staged.get(i["id"])
        actual = i["state"] != "pending" or (it is not None and it["sha256"] == i["sha256"])
        items.append({"n": i["n"], "card": i["id"], "state": i["state"] if actual else "changed",
                      "diff": "\n".join(it["diff"]) if it else "",
                      "staged": it["text"] if actual and i["state"] == "pending" else ""})
    write_json(run_dir / "input.json", {"reply": mail.reply_text(msg), "items": items})
    prompt = session.render(PROMPT.read_text(encoding="utf-8"), input=run_dir / "input.json")
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    start = gate.position(gate.history(ws))
    res = session.launch(cfg, run_dir, prompt, cfg["mail"]["timeout_minutes"])
    events = gate.after(gate.history(ws), start)
    answer = decisions(session.final_text(res["output"])) if res["status"] == "ok" else None
    if answer is None:
        why = res["status"] if res["status"] != "ok" else "解读结果里没有决定"
        results, note = {}, f"没能读懂这封回复（{why}），这次什么都没执行，请再回复一次。"
    elif events:  # the session was only to read; anything it may have written makes its decisions suspect
        results, note = {}, f"解读回复期间记忆库里出现了 {len(events)} 条写入，为安全起见这次什么都没执行，请再回复一次。"
    else:
        sha = {card: it["sha256"] for card, it in staged.items()}
        results, note = execute(cfg, review, answer["decisions"], sha), str(answer.get("message") or "")
    text = confirmation(cfg, review, results, note)
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    write_json(run_dir / "meta.json", {"reply": str(msg["Message-ID"]).strip(), "status": res["status"],
                                       "session_id": res["session_id"], "events": len(events), "decisions": answer,
                                       "results": results})
    counts = Counter(outcome for outcome, _ in results.values())
    line = "处理了一封回复：" + ("，".join(f"{OUTCOMES[o]} {counts[o]}" for o in OUTCOMES if counts[o]) or note)
    return mail.compose(cfg, f"Re: {review['subject']}", text, reply_to=msg), line


def modify(engine: Workspace, item: dict, edits, quote) -> str | None:
    """The user's change, as a patch on the staged version; the item follows the new version. Returns why it failed."""
    if not isinstance(edits, list) or not edits or not all(isinstance(e, dict) and isinstance(e.get("old"), str) and isinstance(e.get("new"), str)
                            for e in edits):
        return "解读结果里没有可执行的替换"
    reason = f"{REVIEW_PREFIX} 用户回复审阅邮件：{' '.join(str(quote or '').split())[:200]}"
    try:
        result = engine.write(item["id"], op="patch", edits=edits, stage=True, reason=reason, if_match=item["sha256"])
    except MindSliceError as exc:
        return str(exc)
    item["sha256"] = result["sha256"]
    return None


def conflict_text(conflict: dict, numbers: dict[str, int]) -> str:
    if conflict.get("reason") == "dependency":
        target = conflict.get("target", "")
        n = numbers.get(gate.card_path(target))
        return f"它引用的 {target}" + (f"（#{n}）" if n else " ") + "还没生效，要一起批准"
    return conflict.get("detail") or conflict.get("reason") or "冲突"


def execute(cfg: dict, review: dict, decided: list, staged: dict[str, str]) -> dict[int, tuple[str, str]]:
    """Carry out the decisions on the items of this mail whose staged version is still the one mailed (or the one an
    earlier reply's modification left). Returns {n: (outcome, detail)} for every item the decisions name."""
    with Workspace(cfg["memory"]["workspace"]) as engine, engine.lock.exclusive():
        snapshot = engine.diff()
        current = {it["id"]: it for it in snapshot["items"]}
        matched = {}
        for item in review["items"]:
            card = item["id"]
            if card in staged and card in current and current[card]["staged_sha256"] == staged[card]:
                if "change_ids" not in item or current[card].get("change_ids", []) == item["change_ids"]:
                    matched[card] = staged[card]
        return _execute_locked(engine, review, decided, matched)


def _execute_locked(engine, review, decided, staged):
    by_n = {i["n"]: i for i in review["items"]}
    results, approve, discard, modified = {}, {}, {}, set()
    counts = Counter(d["n"] for d in decided if isinstance(d, dict) and type(d.get("n")) is int)
    for d in decided:
        item = by_n.get(d["n"]) if isinstance(d, dict) and type(d.get("n")) is int else None
        if item is None:
            continue
        n, action = item["n"], d.get("action")
        if item["state"] != "pending":
            results[n] = ("skipped", f"之前的回复已经处理过（{OUTCOMES[item['state']]}）")
        elif counts[n] > 1:
            results[n] = ("skipped", "同一编号出现多个决定，请重新回复")
        elif item["id"] not in staged or staged[item["id"]] != item["sha256"]:
            results[n] = ("skipped", "发信后暂存版本变过，或者已在别处处理，请以最新的审阅邮件为准")
        elif action == "approve":
            approve[item["id"]] = n
        elif action == "discard":
            discard[item["id"]] = n
        elif action == "modify":
            if error := modify(engine, item, d.get("edits"), d.get("quote")):
                results[n] = ("pending", f"没改成：{error}")
            else:
                approve[item["id"]] = n
                modified.add(n)
        else:
            results[n] = ("pending", str(d.get("note") or ""))

    numbers = {i["id"]: i["n"] for i in review["items"]}
    snapshot = engine.diff()
    revision = snapshot["revision"]
    for it in snapshot["items"]:
        n = numbers.get(it["id"])
        if n in modified:
            by_n[n]["change_ids"] = it.get("change_ids", [])
    while approve or discard:
        try:
            engine.review(list(approve), list(discard), revision=revision)
        except MindSliceError as exc:
            held = {}
            for conflict in exc.details.get("conflicts", []):
                path = gate.card_path(conflict.get("address") or "")
                if path in approve:
                    held.setdefault(path, []).append(conflict_text(conflict, numbers))
            if not held:
                results.update((n, ("pending", str(exc))) for n in [*approve.values(), *discard.values()])
                break
            for path, details in held.items():
                results[approve.pop(path)] = ("pending", "没生效：" + "；".join(details))
        else:
            results.update((n, ("modified" if n in modified else "applied", "")) for n in approve.values())
            results.update((n, ("discarded", "")) for n in discard.values())
            break
    for n, (outcome, _) in results.items():
        if outcome in HANDLED:
            by_n[n]["state"] = outcome
    return results


def confirmation(cfg: dict, review: dict, results: dict, note: str) -> str:
    """The answer in the review thread: what took effect (modified cards with their final text), what was dropped,
    what stays pending and why, and what is still waiting."""
    by_n = {i["n"]: i for i in review["items"]}

    def name(n: int) -> str:
        return f"#{n} {by_n[n]['id']}"

    done = sorted(results.items())
    out = []
    if applied := [name(n) for n, (x, _) in done if x == "applied"]:
        out.append(f"{OUTCOMES['applied']}：{'、'.join(applied)}")
    if modified := [n for n, (x, _) in done if x == "modified"]:
        read = gate.mindslice_json(cfg, "read", *[by_n[n]["id"] for n in modified])
        cards = read.get("cards", [read])  # one address reads as the card itself
        for n, card in zip(modified, cards):
            final = f"{card.get('summary', '')}\n\n{(card.get('body') or '').rstrip()}"
            out.append(f"{OUTCOMES['modified']}：{name(n)}，最终文本：\n\n{final}")
    if discarded := [name(n) for n, (x, _) in done if x == "discarded"]:
        out.append(f"{OUTCOMES['discarded']}：{'、'.join(discarded)}")
    for o in ("pending", "skipped"):
        if rows := [f"- {name(n)}" + (f"：{d}" if d else "") for n, (x, d) in done if x == o]:
            out.append(f"{OUTCOMES[o]}：\n" + "\n".join(rows))
    if note:
        out.append(note)
    left = [f"#{i['n']}" for i in review["items"] if i["state"] == "pending"]
    out.append(f"还有 {len(left)} 处待审：{'、'.join(left)}。直接回复这封邮件继续。" if left
               else "这封审阅邮件里的改动都处理完了。")
    return "\n\n".join(out) + "\n"
