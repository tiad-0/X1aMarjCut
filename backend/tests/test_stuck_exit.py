"""1.2.49: SharkyClips didn't open again after an update.

The server hung while exiting with Whisper on the GPU (Windows ExitProcess runs the CUDA DLLs'
detach code), so the `serve` supervisor waited for it forever and kept the server lock; every
new start said "Another SharkyClips is still starting or not responding". Now the server exits
hard, the supervisor ends a server that's still there after it said it exits, and a start ends
a lock holder that doesn't answer (only a SharkyClips on the same settings folder)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

import cli
from sharkyclips import config, instance
from sharkyclips.web.jobs import JobManager

BACKEND = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A stand-in for `cli.py serve` (the name and the argument are what is_sharkyclips looks at).
FAKE_CLI = (
    "import sys, time; sys.path.insert(0, {backend!r}); from sharkyclips import instance\n"
    "if sys.argv[2] == 'hold':\n"
    "    f = instance.acquire(); instance.note(1)\n"
    "elif sys.argv[2] == 'exiting':\n"
    "    instance.note_exit(75)\n"
    "print('ready', flush=True)\n"
    "time.sleep(600)\n"
)


@pytest.fixture
def fake_cli(tmp_path):
    script = tmp_path / "cli.py"
    script.write_text(FAKE_CLI.format(backend=str(BACKEND)), encoding="utf-8")
    started: list[subprocess.Popen] = []

    def start(mode: str, env: dict | None = None) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, str(script), "serve", mode], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, **(env or {})}, creationflags=NO_WINDOW)
        assert p.stdout.readline().strip() == "ready"
        started.append(p)
        return p

    yield start
    for p in started:
        if p.poll() is None:
            p.kill()
            p.wait(5)
    instance.clear_exit()
    (config.config_dir() / instance.INFO_FILE).unlink(missing_ok=True)


def test_supervisor_ends_a_server_stuck_after_it_said_it_exits(fake_cli):
    p = fake_cli("exiting")
    t = time.monotonic()
    assert cli._wait_server(p, grace=0.5) == 75  # the restart it meant
    assert p.poll() is not None
    assert time.monotonic() - t < 15
    assert instance.exit_note(p.pid) is None  # cleared


def test_supervisor_leaves_a_working_server_alone(fake_cli):
    p = fake_cli("plain")
    done: list[int] = []
    threading.Thread(target=lambda: done.append(cli._wait_server(p, grace=0.2)), daemon=True).start()
    time.sleep(2.5)
    assert p.poll() is None and not done  # no exit note: nobody kills it
    p.kill()
    p.wait(5)


def test_a_start_ends_a_hung_lock_holder(fake_cli):
    fake_cli("hold")
    assert instance.acquire() is None
    assert instance.take_over(instance.running() or {}) is True
    mine = instance.acquire(wait=5)
    assert mine is not None
    instance.release(mine)


def test_take_over_never_touches_other_programs():
    """The lock holder isn't `cli.py serve|launch` (here: python -c): left alone."""
    hold = ("import sys; sys.path.insert(0, sys.argv[1]); from sharkyclips import instance\n"
            "f = instance.acquire(); instance.note(1); print('held', flush=True); sys.stdin.read()\n")
    p = subprocess.Popen([sys.executable, "-c", hold, str(BACKEND)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, creationflags=NO_WINDOW)
    try:
        assert p.stdout.readline().strip() == "held"
        assert instance.take_over(instance.running() or {}) is False
        assert p.poll() is None
    finally:
        p.kill()
        p.wait(5)
        (config.config_dir() / instance.INFO_FILE).unlink(missing_ok=True)


def test_orphan_servers_of_this_settings_folder_are_ended(fake_cli, tmp_path):
    mine = fake_cli("plain", {"SHARKYCLIPS_SUPERVISED": "1"})
    other = fake_cli("plain", {"SHARKYCLIPS_SUPERVISED": "1", "SHARKYCLIPS_HOME": str(tmp_path / "elsewhere")})
    not_server = fake_cli("plain")  # a `serve` supervisor / launcher, not a web server child
    # a venv python is a launcher + the real interpreter as its child: either may be the one found
    found = instance.clean_orphans()
    assert found and other.pid not in found and not_server.pid not in found
    mine.wait(10)
    assert other.poll() is None and not_server.poll() is None


def test_hard_exit_ends_the_process_and_its_children(tmp_path):
    code = ("import subprocess, sys, time; sys.path.insert(0, sys.argv[1]); from sharkyclips import procs\n"
            "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
            "print(kid.pid, flush=True); time.sleep(0.5); procs.hard_exit(7)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(BACKEND)], stdout=subprocess.PIPE, text=True,
                         creationflags=NO_WINDOW)
    kid = int(p.stdout.readline())
    assert p.wait(20) == 7
    assert not psutil.pid_exists(kid) or psutil.Process(kid).status() == psutil.STATUS_ZOMBIE


def test_queue_saves_from_many_threads_never_collide(tmp_path, capsys):
    """Was: 'Could not save the job queue: Permission denied: _queue.json.tmp'."""
    store = tmp_path / "_queue.json"
    m = JobManager(store=store)
    threads = [threading.Thread(target=lambda: [m._save() for _ in range(40)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert "Could not save" not in capsys.readouterr().out
    assert json.loads(store.read_text(encoding="utf-8"))["version"] == 1
    assert [f.name for f in tmp_path.iterdir()] == ["_queue.json"]  # no temp files left
