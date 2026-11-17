"""Stand-in for agy reading a review reply: answers with the decisions in FAKE_DECISIONS (JSON), in a code fence.

FAKE_REVIEW=write also writes to memory, as a session that strayed would; FAKE_REVIEW=fail exits non-zero.
The answer carries the task it was given under "seen"."""

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
if mode == "write":
    subprocess.run([shutil.which("mindslice"), "-w", os.environ["MINDSLICE_WORKSPACE"], "write", "projects/demo/stray",
                    "--title", "Stray", "--summary", "A stray write.", "--content", "- s", "--reason", "strayed"],
                   check=True, capture_output=True)
answer = os.environ.get("FAKE_DECISIONS", '{"decisions": []}')
print(json.dumps({"conversation_id": "fake-review-1", "response": f"好的：\n```json\n{answer}\n```", "seen": task}))
