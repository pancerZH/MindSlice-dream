"""Whole-card drop proposals (PLAN decision 15). The dream writes them to drops.json; the program checks their form,
keeps the open ones in state.json across runs and works out from the engine what dropping each would touch. Dropping
stays the user's: a Claude session carries out the ones the user approves (`mindslice bake @slice --drop <path>`)."""

import json
from datetime import date
from pathlib import Path

from . import gate, session

FILE = "drops.json"


def _texts(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value)


def _moves(value) -> bool:
    return isinstance(value, list) and all(isinstance(m, dict) and all(isinstance(m.get(k), str) and m[k].strip()
                                                                       for k in ("text", "to")) for m in value)


def proposals(path: Path, drops: dict, exclude: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """The session's drops.json: the proposals in the expected form, and the refused ones with why."""
    if not path.exists():
        return [], []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [], [f"{FILE} 不是合法的 JSON（{e}），一条都没收录"]
    if not isinstance(items, list):
        return [], [f"{FILE} 不是列表，一条都没收录"]
    kept = {k["card"] for k in drops.get("kept", [])}
    found, refused = [], []
    for it in items:
        it = it if isinstance(it, dict) else {}
        card = gate.card_path(it["card"]) if isinstance(it.get("card"), str) else ""
        why = ("没写 card" if not card
               else "没写 reason" if not isinstance(it.get("reason"), str) or not it["reason"].strip()
               else "evidence 至少要有一条依据" if not _texts(it.get("evidence"))
               else "migrate 的每一项都要有 text 和 to" if not _moves(it.get("migrate", []))
               else "用户决定保留这张卡" if card in kept
               else "这张卡由别的流程管" if card.startswith(exclude) else None)
        if why:
            refused.append(f"`{card or '?'}`：{why}")
        else:
            found.append({"card": card, "reason": it["reason"].strip(), "evidence": it["evidence"],
                          "migrate": it.get("migrate", [])})
    return found, refused


def merge(drops: dict, found: list[dict], run_id: str) -> dict:
    """The open proposals after a run: a card proposed again takes the new proposal."""
    pending = {d["card"]: d for d in drops.get("pending", [])} | {d["card"]: d | {"run": run_id} for d in found}
    return {**drops, "pending": list(pending.values())}


def _cards(value: dict) -> list[dict]:
    return value["cards"] if "cards" in value else [value]  # several addresses, or one


def facts(cfg: dict, cards: list[str]) -> dict[str, dict]:
    """Where each card is now and what dropping it would touch: the slices holding it, whether @overlay has a copy
    (it goes first, or the next bake writes the card back), the live cards linking to it and the cards extending it
    (bake --drop refuses while one does). A card no mount holds any more is left out."""
    ws = cfg["memory"]["workspace"]
    mounts = gate.mindslice_json(cfg, "manifest", "--limit", "1000", "--max-folders", "0", "--no-always")["mounts"]
    held = {c: [m["alias"] for m in mounts if (ws / m["path"] / f"{c}.md").is_file()] for c in cards}
    copies = [f"{alias}/{c}" for c, aliases in held.items() for alias in aliases]
    if not copies:
        return {}
    links, extensions = {}, {}
    for c in _cards(gate.mindslice_json(cfg, "read", *copies, "--follow", "none",
                                        "--max-backlinks", "1000", "--max-extensions", "1000")):
        links.setdefault(c["path"], set()).update(b["address"] for b in c.get("backlinks", []))
        extensions.setdefault(c["path"], set()).update(e["address"] for e in c.get("extensions", []))
    # a link counts only from the card that wins at its path: not from a slice card that an @overlay copy replaced
    # (the copy may have dropped the link) or that was superseded
    sources, live = sorted(set().union(*links.values()) - set(copies)), set()
    if sources:
        winners = gate.mindslice_json(cfg, "read", *(f"./{gate.card_path(a)}" for a in sources),
                                      "--max-backlinks", "0", "--max-extensions", "0")
        live = {c["address"] for c in _cards(winners)}
    return {c: {"slices": [a for a in aliases if a != "@overlay"], "overlay": "@overlay" in aliases,
                "linked_from": sorted(links.get(c, set()) & live), "extended_by": sorted(extensions.get(c, set()))}
            for c, aliases in held.items() if aliases}


def refresh(cfg: dict, drops: dict) -> tuple[dict, dict]:
    """The proposals still open (a card no mount holds any more was dropped or deleted), and their facts."""
    found = facts(cfg, [d["card"] for d in drops.get("pending", [])])
    return {**drops, "pending": [d for d in drops.get("pending", []) if d["card"] in found]}, found


def section(drops: dict, found: dict, refused: list[str]) -> str:
    """The report's list of open drop proposals, also mailed with it. Not numbered: the mail's numbers are the
    staged changes a reply decides on, and drops are not decided by mail."""
    if not drops.get("pending") and not refused:
        return ""
    lines = ["", "## 可以整张删掉的卡（程序整理）", "",
             "dream 提出、还没处理的整卡删除建议，包括以前的运行提的；dream 不删卡、不 bake，回复邮件也不处理它们。"
             "要处理时，在 Claude 会话里说「处理 dream 的删除建议」；`mindslice-dream drops` 列出当前状态，"
             "决定保留的用 `mindslice-dream drops --keep <card> --why <原因>` 记下，dream 以后不再提。"]
    for d in drops.get("pending", []):
        f = found[d["card"]]
        where = "、".join(f["slices"]) or "只在 @overlay，用 `write --op delete` 删"
        lines += ["", f"- `{d['card']}`（{where}；{d['run']} 提出）",
                  f"  - 理由：{d['reason']}",
                  f"  - 依据：{'；'.join(d['evidence'])}"]
        lines += [f"  - 删前挪走：「{m['text']}」→ `{m['to']}`" for m in d["migrate"]]
        lines.append(f"  - 还链接它的卡：{'、'.join(f'`{a}`' for a in f['linked_from']) or '无'}")
        if f["extended_by"]:
            lines.append(f"  - 扩展它的卡（不先处理，bake --drop 会拒绝）：{'、'.join(f'`{a}`' for a in f['extended_by'])}")
        if f["overlay"] and f["slices"]:
            lines.append("  - @overlay 里有它的副本：先删掉副本，否则 bake 会把它写回去")
    if refused:
        lines += ["", "dream 的这些建议没有收录：", ""] + [f"- {r}" for r in refused]
    return "\n".join(lines) + "\n"


def after_run(cfg: dict, run_dir: Path, drops: dict, exclude: tuple[str, ...]) -> tuple[dict, str, list[str]]:
    """state.json's drops after a run, the report's section and the refused proposals. A failed run's proposals are
    kept too: they are only proposals, checked again when carried out. Never raises, so that whatever goes wrong here
    does not cost the run its record."""
    found, refused = proposals(run_dir / FILE, drops, exclude)
    drops = merge(drops, found, run_dir.name)
    try:
        drops, facts_now = refresh(cfg, drops)
        refused += [f"`{d['card']}`：没有这张卡" for d in found if d["card"] not in facts_now]
        return drops, section(drops, facts_now, refused), refused
    except Exception as e:
        return drops, (f"\n## 可以整张删掉的卡（程序整理）\n\n没整理出来（{type(e).__name__}: {e}）；"
                       "建议已经记下，用 `mindslice-dream drops` 重看。\n"), refused


def show(cfg: dict, keep: str | None = None, why: str = "") -> str:
    """`drops [--keep CARD --why TEXT]`: the open proposals with current facts, after recording a card the user keeps."""
    with session.locked(cfg, wait=False) as free:
        if not free:
            return "dream 正在运行，稍后再试"
        state = gate.load_state(cfg["paths"]["state"])
        drops = state.get("drops", {})
        if keep:
            card = gate.card_path(keep)
            if not facts(cfg, [card]):
                return f"没有这张卡：{card}"
            drops = {"pending": [d for d in drops.get("pending", []) if d["card"] != card],
                     "kept": [k for k in drops.get("kept", []) if k["card"] != card]
                             + [{"card": card, "why": why, "date": date.today().isoformat()}]}
        drops, found = refresh(cfg, drops)
        gate.save_state(cfg["paths"]["state"], state | {"drops": drops})
        return section(drops, found, []).strip() or "没有待处理的整卡删除建议。"
