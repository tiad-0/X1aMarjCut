"""One SharkyClips server per settings folder.

Two servers on the same settings folder both run the background loops: the publish scheduler
(both export and upload the same scheduled clip), the job queue (both resume it), the stats
sync. 1.2.11 found this for real: a second server started on port 8001 while the first one
restarted after an update, and both rendered the same clip at the same time.

The `serve` supervisor (the long-lived parent of the web server) holds an OS lock on
<settings>/server.lock for its whole life, across server restarts, and notes its port in
<settings>/server.json. The OS drops the lock when the process ends, even after a crash, so a
stale file never blocks a start. A second `serve` / `launch` sees the lock and opens the
running SharkyClips instead of starting another one.

1.2.49 (the app didn't open after an update): the server hung while exiting with Whisper on the
GPU, so `serve` waited for it forever and held the lock. Now
  * the server notes "I'm exiting" (EXIT_FILE) and `serve` ends it from outside when it is still
    there EXIT_GRACE seconds later,
  * a start that finds the lock held by a SharkyClips that doesn't answer ends that one
    (`take_over`) instead of giving up, and
  * the lock holder ends left-over server processes of this settings folder (`clean_orphans`).
Only processes that are SharkyClips (`cli.py serve|launch`) on THIS settings folder are touched.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import IO, Any

import psutil

from . import config

LOCK_FILE = "server.lock"
INFO_FILE = "server.json"
EXIT_FILE = "server_exit.json"
EXIT_GRACE = 15.0  # seconds a server may take to exit after Restart / Quit before it is killed


def _lock(f: IO[Any]) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def acquire(wait: float = 0.0) -> IO[Any] | None:
    """Take the server lock. Returns the open lock file (keep it for the process's life), or None
    when another SharkyClips holds it. `wait`: keep trying this long (Windows releases a dead
    process's lock a moment after it ended, e.g. right after a crash)."""
    folder = config.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    end = time.monotonic() + wait
    while True:
        f = open(folder / LOCK_FILE, "a+")  # noqa: SIM115 - held open on purpose
        if _lock(f):
            return f
        f.close()
        if time.monotonic() >= end:
            return None
        time.sleep(0.1)


def release(f: IO[Any] | None) -> None:
    if f is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()
    (config.config_dir() / INFO_FILE).unlink(missing_ok=True)


def note(port: int) -> None:
    """The lock holder's port, so a second start can open it."""
    path = config.config_dir() / INFO_FILE
    path.write_text(json.dumps({"pid": os.getpid(), "port": port, "since": time.time()}), encoding="utf-8")


def running() -> dict | None:
    """{pid, port, since} of the SharkyClips holding the lock, or None when nobody does."""
    mine = acquire()
    if mine is not None:  # free: nobody runs (release also removes a stale server.json)
        release(mine)
        return None
    try:
        info = json.loads((config.config_dir() / INFO_FILE).read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}  # locked, but the port note isn't written yet (starting up)


# ----------------------------------------------------------------- exiting server


def note_exit(code: int) -> None:
    """The (supervised) server is about to exit with `code`."""
    try:
        (config.config_dir() / EXIT_FILE).write_text(
            json.dumps({"pid": os.getpid(), "code": int(code), "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def exit_note(pid: int) -> dict | None:
    """{pid, code, at} when the server `pid` said it is exiting, else None. The note may come
    from a child of `pid`: a venv's python.exe is a launcher that runs the real interpreter as
    its child, so the pid `serve` started is not the one that writes the note."""
    try:
        note = json.loads((config.config_dir() / EXIT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(note, dict) or not isinstance(note.get("pid"), int):
        return None
    if note["pid"] == pid:
        return note
    try:
        if any(c.pid == note["pid"] for c in psutil.Process(pid).children(recursive=True)):
            return note
    except psutil.Error:
        pass
    return None


def clear_exit() -> None:
    (config.config_dir() / EXIT_FILE).unlink(missing_ok=True)


# ----------------------------------------------------------------- stuck processes


def _home_of(env: dict) -> Path:
    other = env.get("SHARKYCLIPS_HOME") or env.get("CLIPFORGE_HOME")
    if other:
        return Path(other).expanduser()
    home = env.get("USERPROFILE") if os.name == "nt" else env.get("HOME")
    return (Path(home) if home else Path.home()) / ".sharkyclips"


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return False


def is_sharkyclips(p: psutil.Process, server_only: bool = False) -> bool:
    """A SharkyClips `serve` / `launch` process (`server_only`: the supervised web server child)
    working on THIS settings folder. Anything else, or a process we can't inspect: False."""
    try:
        cmd = p.cmdline()
        env = p.environ()
    except (psutil.Error, OSError):
        return False
    if not any(Path(a).name.lower() == "cli.py" for a in cmd) or not ({"serve", "launch"} & set(cmd)):
        return False
    if server_only and env.get("SHARKYCLIPS_SUPERVISED") != "1":
        return False
    return _same_folder(_home_of(env), config.config_dir())


def kill_tree(pid: int, timeout: float = 5.0) -> bool:
    """Kill a process and everything below it (suspended ones are resumed first). True when it's gone."""
    try:
        root = psutil.Process(pid)
        tree = [root, *root.children(recursive=True)]
    except psutil.Error:
        return True
    for p in reversed(tree):  # children first: nothing gets re-parented on the way
        try:
            if p.status() == psutil.STATUS_STOPPED:
                p.resume()
        except (psutil.Error, OSError):
            pass
        try:
            p.kill()
        except (psutil.Error, OSError):
            pass
    _gone, alive = psutil.wait_procs(tree, timeout=timeout)
    return not any(p.pid == pid for p in alive)


def clean_orphans() -> list[int]:
    """Called by the lock holder before it starts its server: any other web server process of this
    settings folder is left over from a supervisor that died or hung (it would keep the port, the
    GPU and the job queue busy). Ends them; returns their pids."""
    me = os.getpid()
    found = []
    for p in psutil.process_iter():
        if p.pid != me and is_sharkyclips(p, server_only=True):
            found.append(p.pid)
            kill_tree(p.pid)
    return found


def take_over(info: dict) -> bool:
    """The lock holder doesn't answer at all (it hangs, e.g. waiting for a server stuck while
    exiting): end it and everything it started, so this start can take the lock. Only a
    SharkyClips on this settings folder is ended. True when one was ended."""
    me = os.getpid()
    try:
        born = psutil.Process(me).create_time()
    except psutil.Error:
        born = time.time()
    noted = isinstance(info.get("pid"), int)
    pids = [info["pid"]] if noted else []
    if not noted:  # the port note is missing: look for the holder
        pids = [p.pid for p in psutil.process_iter() if p.pid != me and is_sharkyclips(p)]
    ended = False
    for pid in pids:
        if pid == me:
            continue
        try:
            p = psutil.Process(pid)
            # a guess (no note) must have started before us: a 2nd click right now didn't hang
            older = noted or p.create_time() <= born
        except psutil.Error:
            continue
        if older and is_sharkyclips(p):
            ended = kill_tree(pid) or ended
    return ended
