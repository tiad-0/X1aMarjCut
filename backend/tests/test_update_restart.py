"""1.2.51: "Restart now" really brings the new version up.

A copy of SharkyClips (this backend + a VERSION file) runs under the real `cli.py serve`
supervisor. The VERSION on disk is raised under the running server (what an update does): the
server reports restart_needed, POST /api/update/restart, and the supervisor starts it again on
the new code. An equal / lower VERSION on disk never asks for a restart."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil
import pytest

BACKEND = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - not up (yet)
        return None


def _wait(port: int, cond, limit: float = 90.0) -> dict | None:
    end = time.time() + limit
    while time.time() < end:
        st = _get(port, "/api/app")
        if st is not None and cond(st):
            return st
        time.sleep(0.5)
    return None


@pytest.fixture
def install(tmp_path):
    app = tmp_path / "app"
    shutil.copytree(BACKEND / "sharkyclips", app / "backend" / "sharkyclips",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(BACKEND / "cli.py", app / "backend" / "cli.py")
    (app / "frontend" / "dist").mkdir(parents=True)
    (app / "frontend" / "dist" / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (app / "VERSION").write_text("1.2.50\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    port = _free_port()
    env = {**os.environ, "SHARKYCLIPS_HOME": str(home), "SHARKYCLIPS_NO_MIGRATE": "1",
           "SHARKYCLIPS_DOWNLOADS": str(home / "dl"), "SHARKYCLIPS_QUEUE_HOME": str(home / "q"),
           "PYTHONIOENCODING": "utf-8"}
    env.pop("SHARKYCLIPS_SUPERVISED", None)
    proc = subprocess.Popen([sys.executable, str(app / "backend" / "cli.py"), "serve", "--no-browser",
                             "--port", str(port)], cwd=str(app), env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        yield app, port, proc
    finally:
        try:
            parent = psutil.Process(proc.pid)
            kids = parent.children(recursive=True)
            for p in [*kids, parent]:
                try:
                    p.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs([*kids, parent], timeout=10)
        except psutil.Error:
            pass


def test_restart_now_comes_back_on_the_new_version(install):
    app, port, _proc = install
    st = _wait(port, lambda s: s.get("version") == "1.2.50")
    assert st is not None, "the server didn't start"
    assert st["restart_needed"] is False and st["disk_version"] == "1.2.50"

    (app / "VERSION").write_text("1.2.49\n", encoding="utf-8")  # an older file: never a restart
    assert _get(port, "/api/app")["restart_needed"] is False

    (app / "VERSION").write_text("1.2.51\n", encoding="utf-8")  # the update changed the files
    st = _get(port, "/api/app")
    assert st["version"] == "1.2.50" and st["disk_version"] == "1.2.51" and st["restart_needed"] is True

    token = (app.parent / "home" / "api_token").read_text(encoding="ascii").strip()  # like the tray (web/security.py)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/update/restart", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "X-SharkyClips-Token": token})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read().decode("utf-8"))["restarting"] is True

    st = _wait(port, lambda s: s.get("version") == "1.2.51")
    assert st is not None, "the server didn't come back on the new version"
    assert st["restart_needed"] is False  # no second restart, no loop
