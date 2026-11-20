"""Stand-in for agy: reads the rendered dream prompt and acts like a dream session.

By default it patches the first card of new_writes, sends one --also batch (append + create), writes the
report and answers with a session id; it writes through the prompt's `ms` without --stage, as the guard stages for it,
and leaves a stray file in the run directory. FAKE_DREAM changes that:
- delete | invest | apply | nostage | noreason | alsodelete: a call the guard refuses (what it said is in "refused");
- bypass | bypassapply | always | concurrent | error: what only the post-run checks see: a call to another mindslice
  on PATH (an unstaged write; apply; --no-stage to an always-loaded card; another session's write) or a broken file
  written directly; repofile | repocommit: a file left in a watched repo, or a commit there;
- noreport | answer | fail | sleep | straggler.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

prompt = sys.argv[1]
ms = shlex.split(re.search(r"记忆命令：`([^`]+)`", prompt).group(1))
workspace = Path(re.search(r"记忆工作区：`([^`]+)`", prompt).group(1))
task = json.loads(Path(re.search(r"任务输入：`([^`]+)`", prompt).group(1)).read_text())
report = Path(re.search(r"报告：写到 `([^`]+)`", prompt).group(1))
mode = os.environ.get("FAKE_DREAM", "")
# the first mindslice on PATH, and the next one: the CLI the guard runs, reached around it
other = [shutil.which("mindslice", path=os.environ["PATH"].split(os.pathsep, 1)[1]), "-w", str(workspace)]
seen = {"cwd": os.getcwd(), "path_mindslice": shutil.which("mindslice"), "ms": ms[0],
        "env": {k: os.environ.get(k) for k in ("MINDSLICE_PLUGIN_ISOLATE", "MINDSLICE_WORKSPACE", "PYTHONDONTWRITEBYTECODE")}}
DREAM = "[dream] fake: checked against git log"


def write(card: str, *args: str, reason: str = DREAM, cli: list[str] = ms) -> None:
    subprocess.run([*cli, "write", card, *args, "--reason", reason], check=True, capture_output=True)


def refused(*argv: str) -> None:
    res = subprocess.run([*ms, *argv], capture_output=True, text=True)
    seen["refused"] = {"code": res.returncode, "stderr": res.stderr.strip()}


if mode == "sleep":
    print(json.dumps({"pid": os.getpid()}), flush=True)
    time.sleep(30)
if mode == "fail":
    sys.exit(3)

card = task["new_writes"][0]["card"]
write(card, "--op", "patch", "--edit", "uncommitted", "committed in abc1234")
Path("batch.json").write_text(json.dumps([{"id": "projects/demo/notes", "op": "create", "title": "Demo notes",
                                           "summary": "Notes split out of the demo status card.",
                                           "content": "- Split out by the dream pass."}]))
write(card, "--op", "append", "--content", "- Verified 2026-11-18 against git log", "--also", "batch.json")

if mode == "delete":
    refused("write", card, "--op", "delete", "--reason", DREAM)
elif mode == "invest":
    refused("write", "invest/theses/x", "--title", "x", "--summary", "x.", "--content", "- x", "--reason", DREAM)
elif mode == "apply":
    refused("apply")
elif mode == "nostage":
    refused("write", card, "--op", "append", "--content", "- More.", "--no-stage", "--reason", DREAM)
elif mode == "noreason":
    refused("write", card, "--op", "append", "--content", "- More.", "--reason", "checked")
elif mode == "alsodelete":
    Path("bad.json").write_text(json.dumps([{"id": "projects/demo/notes", "op": "delete"}]))
    refused("write", card, "--op", "append", "--content", "- More.", "--also", "bad.json", "--reason", DREAM)
elif mode == "bypass":
    write(card, "--op", "append", "--content", "- Unstaged.", cli=other)
elif mode == "bypassapply":
    subprocess.run([*other, "apply"], check=True, capture_output=True)
elif mode == "error":
    (workspace / "overlay" / "broken.md").write_text("+++\ntitle = \n+++\n")
elif mode == "always":
    write("prefs/p", "--op", "append", "--content", "- Also be kind.", "--no-stage", cli=other)
elif mode == "straggler":  # a background process in its own session, still running when the session ends
    seen["straggler"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True).pid
elif mode in ("repofile", "repocommit"):
    repo = task["repos"][0]["path"]
    Path(repo, "left.txt").write_text("left behind")
    if mode == "repocommit":
        subprocess.run(["git", "-C", repo, "add", "left.txt"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "x"],
                       check=True)
elif mode == "concurrent":
    write("projects/demo/other", "--title", "Other", "--summary", "Another session's card.", "--content", "- o",
          reason="Another session recorded this.", cli=other)

if mode not in ("noreport", "answer"):
    report.write_text("# Dream 报告\n\n- 查了 1 张卡，1 张过期，已改正。\n")
(report.parent / "notes.txt").write_text("scratch written next to the report")
answer = {"answer": {"response": "# 来自最终回答的报告\n"}, "noreport": {}}.get(mode, {"response": "done"})
print(json.dumps({"session_id": "fake-dream-1", **answer, **seen}))
