"""mindslice-dream run [--force] [--dry-run]: the nightly dream pass over a MindSlice workspace."""

import argparse
import json
import shutil
import sys
import tomllib
from pathlib import Path

from . import runner

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
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mindslice-dream", description=__doc__)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="gate, then one dream session and the post-run checks")
    p.add_argument("--force", action="store_true", help="start the session even if the gate finds nothing to do")
    p.add_argument("--dry-run", action="store_true", help="only run the gate and print input.json")
    args = ap.parse_args(argv)

    meta = runner.run(load_config(args.config), force=args.force, dry_run=args.dry_run)
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
    return 0 if meta["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
