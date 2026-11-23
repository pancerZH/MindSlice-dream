"""mindslice-dream run [--force] [--dry-run] | mail [--send] | drops [--keep CARD --why TEXT]: the nightly dream pass
over a MindSlice workspace, the review of its staged changes by email, and its open whole-card drop proposals."""

import argparse
import json
import shutil
import sys
import tomllib
from pathlib import Path

from . import drops, review, runner

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.toml"


def load_config(path: Path) -> dict:
    """config.toml with its paths made absolute (relative ones are relative to the config file's directory)."""
    if not path.exists():
        sys.exit(f"{path} not found: copy config.example.toml to config.toml and edit it")
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.resolve().parent

    def resolve(p: str) -> Path:
        return (base / Path(p).expanduser()).resolve()

    mem = cfg["memory"]
    mem["workspace"] = resolve(mem["workspace"])
    found = shutil.which(mem["mindslice"])
    if not found:
        sys.exit(f"mindslice CLI not found: {mem['mindslice']}")
    mem["mindslice"] = found
    cfg["paths"] = {k: resolve(v) for k, v in cfg["paths"].items()}
    cfg["repos"] = [resolve(r) for r in cfg["watch"]["repos"]]
    cfg["dream"].setdefault("max_card_chars", 5000)  # configs written before the setting existed
    cfg.setdefault("mail", {"to": ""})  # no [mail] section: no review mails
    if "account" in cfg["mail"]:
        cfg["mail"]["account"] = resolve(cfg["mail"]["account"])
    sidecar = cfg.setdefault("sidecar", {})
    sidecar["cmd"] = [str(Path(a).expanduser()) if a.startswith("~") else a for a in sidecar.get("cmd", [])]
    sidecar.setdefault("k", 3)
    sidecar.setdefault("min_score", 0.65)
    sidecar.setdefault("top_pairs", 15)
    sidecar.setdefault("timeout_seconds", 120)
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mindslice-dream", description=__doc__)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="gate, then one dream session and the post-run checks")
    p.add_argument("--force", action="store_true", help="start the session even if the gate finds nothing to do")
    p.add_argument("--dry-run", action="store_true", help="only run the gate and print input.json")
    p = sub.add_parser("mail", help="carry out the replies to the review mail (what the 10-minute timer runs)")
    p.add_argument("--send", action="store_true", help="mail the pending dream changes now instead")
    p = sub.add_parser("drops", help="list the open whole-card drop proposals with what dropping each would touch")
    p.add_argument("--keep", metavar="CARD", help="record that the user keeps this card: dream proposes it no more")
    p.add_argument("--why", default="", help="why the user keeps it (required with --keep)")
    args = ap.parse_args(argv)
    if args.cmd == "drops" and args.keep and not args.why.strip():
        ap.error("--keep needs --why")

    cfg = load_config(args.config)
    if args.cmd == "drops":
        print(drops.show(cfg, args.keep, args.why.strip()))
        return 0
    if args.cmd == "mail":
        if args.send:
            result = review.send_now(cfg)
            print(f"mail: {result}")
            return 1 if result.startswith("failed") else 0
        for line in review.poll(cfg):
            print(f"mail: {line}")
        return 0

    meta = runner.run(cfg, force=args.force, dry_run=args.dry_run)
    if meta["status"] == "dry-run":
        print(json.dumps(meta["input"], ensure_ascii=False, indent=2))
        return 0
    if meta["status"] == "skipped":
        print("dream: skipped, nothing to do")
        return 0
    print(f"dream: {meta['status']} · triggers: {', '.join(meta['triggers'])}\n  report: {meta['report']}")
    for line in meta["problems"] + meta["warnings"]:
        print(f"  ! {line}")
    if meta["resume"]:
        print(f"  resume: {meta['resume']}")
    print(f"  mail: {meta['mail']}")
    return 0 if meta["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
