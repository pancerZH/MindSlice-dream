"""Stand-in for agy reading a review reply: answers with the decisions in FAKE_DECISIONS (JSON), in a code fence.

FAKE_REVIEW=write also writes to memory, as a session that got around its read-only mindslice would (through the
next one on PATH); FAKE_REVIEW=guarded tries to write through the mindslice it was given (what that said is in
"refused"); FAKE_REVIEW=fail exits non-zero. The answer carries the task it was given under "seen"."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

prompt = sys.argv[1]
task = json.loads(Path(re.search(r"输入文件：`([^`]+)`", prompt).group(1)).read_text())
mode = os.environ.get("FAKE_REVIEW", "")
if mode == "fail":
    sys.exit(3)
stray = ["-w", os.environ["MINDSLICE_WORKSPACE"], "write", "projects/demo/stray", "--title", "Stray",
         "--summary", "A stray write.", "--content", "- s", "--reason", "[dream] strayed"]
refused = None
if mode == "write":
    other = shutil.which("mindslice", path=os.environ["PATH"].split(os.pathsep, 1)[1])
    subprocess.run([other, *stray], check=True, capture_output=True)
elif mode == "guarded":
    res = subprocess.run([shutil.which("mindslice"), *stray], capture_output=True, text=True)
    refused = {"code": res.returncode, "stderr": res.stderr.strip()}
answer = os.environ.get("FAKE_DECISIONS", '{"decisions": []}')
print(json.dumps({"conversation_id": "fake-review-1", "response": f"好的：\n```json\n{answer}\n```", "seen": task,
                  "refused": refused}))
