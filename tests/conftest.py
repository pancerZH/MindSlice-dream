import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mindslice_dream import cli

ROOT = Path(__file__).resolve().parents[1]
MS = shutil.which("mindslice")


def ms(ws: Path, *args: str) -> str:
    return subprocess.run([MS, "-w", str(ws), *args], check=True, capture_output=True, text=True).stdout


def commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "commit", "-q", "--allow-empty", "-m", message], check=True)


@pytest.fixture
def ws(tmp_path) -> Path:
    """A scratch workspace (never ~/mindslice) holding one status card written by an ordinary session."""
    path = tmp_path / "mindslice"
    subprocess.run([MS, "init", str(path)], check=True, capture_output=True)
    ms(path, "write", "projects/demo/status", "--title", "Demo status", "--summary", "Where the demo project stands.",
       "--content", "- State: the fix is uncommitted.", "--reason", "User asked to record the demo state.")
    return path


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    commit(path, "first")
    return path


@pytest.fixture
def cfg(tmp_path, ws, repo) -> dict:
    """The committed config template, pointed at the scratch workspace, scratch state and runs, and the fake harness."""
    cfg = cli.load_config(ROOT / "config.example.toml")
    cfg["memory"]["workspace"] = ws
    cfg["paths"] = {"runs": tmp_path / "runs", "state": tmp_path / "state.json"}
    cfg["repos"] = [repo]
    cfg["harness"] |= {"cmd": [sys.executable, str(ROOT / "tests" / "fake_harness.py"), "{prompt}"],
                       "timeout_minutes": 1}
    return cfg
