"""The self-update flow against a local "GitHub" (a bare repository in a temp folder)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sharkyclips import __version__, updater

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit_version(repo: Path, version: str, message: str, tag: bool = True) -> None:
    (repo / "VERSION").write_text(version + "\n", encoding="utf-8")
    (repo / "backend").mkdir(exist_ok=True)
    (repo / "backend" / "requirements.txt").write_text("rich\n", encoding="utf-8")
    run(repo, "add", "-A")
    run(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message)
    if tag:  # 1.2.52: the updater installs only tagged releases
        run(repo, "tag", "-f", f"v{version}")


@pytest.fixture(autouse=True)
def _local_remote_is_official(monkeypatch, tmp_path):
    """The bare repo in tmp_path plays GitHub (updater.official_remote)."""
    monkeypatch.setenv("SHARKYCLIPS_UPDATE_REPO", str(tmp_path / "remote.git"))


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    author = tmp_path / "author"
    user = tmp_path / "user"
    run(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    run(tmp_path, "clone", "-q", str(remote), str(author))
    run(author, "checkout", "-q", "-b", "main")
    commit_version(author, __version__, "First release")
    run(author, "push", "-q", "--tags", "-u", "origin", "main")
    run(tmp_path, "clone", "-q", str(remote), str(user))
    return author, user


def test_parse_version():
    assert updater.parse_version('__version__ = "1.2.3"') == "1.2.3"
    assert updater.parse_version("1.2.1\n") == "1.2.1"
    assert updater.parse_version("nothing") is None
    assert updater.version_tuple("1.10.0") > updater.version_tuple("1.9.9")


def test_not_a_checkout(tmp_path):
    st = updater.status(root=tmp_path)
    assert st["supported"] is False and st["reason"] == "not_git_checkout"
    assert st["current"] == __version__


def test_up_to_date(repos):
    _, user = repos
    st = updater.status(root=user)
    assert st["supported"] and not st["available"] and st["behind"] == 0


def test_update_available_and_apply(repos):
    author, user = repos
    commit_version(author, "9.9.9", "Faster rendering")
    run(author, "push", "-q", "--tags", "origin", "main")
    st = updater.status(root=user)
    assert st["available"] and st["behind"] == 1
    assert st["latest"] == "9.9.9"
    assert st["changes"] == ["Faster rendering"]

    stages = []
    res = updater.apply(stages.append, root=user)
    assert res["changed"] and res["version"] == "9.9.9" and not res["packages_updated"]
    assert stages == ["update_download"]
    assert updater.status(root=user)["available"] is False


def test_apply_refuses_conflicting_local_changes(repos):
    author, user = repos
    commit_version(author, "9.9.9", "New version")
    run(author, "push", "-q", "--tags", "origin", "main")
    (user / "VERSION").write_text("# edited by hand\n", encoding="utf-8")
    updater.status(root=user)
    with pytest.raises(updater.SharkyClipsError):
        updater.apply(root=user)


# ------------------------------------------------------------------ 1.2.51: semver, no downgrade, restart


@pytest.fixture
def at_1250(tmp_path, monkeypatch):
    """A user checkout running 1.2.50, and the author's clone that pushes the "remote" version."""
    remote = tmp_path / "remote.git"
    author = tmp_path / "author"
    user = tmp_path / "user"
    run(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    run(tmp_path, "clone", "-q", str(remote), str(author))
    run(author, "checkout", "-q", "-b", "main")
    commit_version(author, "1.2.50", "SharkyClips 1.2.50")
    run(author, "push", "-q", "--tags", "-u", "origin", "main")
    run(tmp_path, "clone", "-q", str(remote), str(user))
    monkeypatch.setattr(updater, "__version__", "1.2.50")
    return author, user


def push_version(author: Path, version: str) -> None:
    commit_version(author, version, f"SharkyClips {version}")
    run(author, "push", "-q", "--tags", "origin", "main")


def test_semver_compare():
    assert updater.is_newer("1.2.51", "1.2.50")
    assert updater.is_newer("1.2.10", "1.2.9")  # numbers, not text
    assert updater.is_newer("1.3.0", "1.2.99")
    assert not updater.is_newer("1.2.49", "1.2.50")
    assert not updater.is_newer("1.2.50", "1.2.50")
    assert not updater.is_newer(None, "1.2.50")
    assert updater.version_tuple("1.2") == (1, 2, 0)


def test_remote_older_shows_no_update(at_1250):
    """current 1.2.50 / remote 1.2.49 -> no notification (the remote moved backwards)."""
    author, user = at_1250
    push_version(author, "1.2.49")
    st = updater.status(root=user)
    assert st["behind"] == 1 and st["latest"] == "1.2.49"
    assert st["available"] is False and st["reason"] == "not_newer"


def test_remote_same_version_shows_no_update(at_1250):
    """current 1.2.50 / remote 1.2.50 (e.g. a CHANGELOG fix commit) -> no notification."""
    author, user = at_1250
    (author / "CHANGELOG.md").write_text("typo fixed\n", encoding="utf-8")
    run(author, "add", "-A")
    run(author, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "CHANGELOG typo")
    run(author, "push", "-q", "--tags", "origin", "main")
    st = updater.status(root=user)
    assert st["behind"] == 1 and st["available"] is False


def test_remote_newer_shows_update(at_1250):
    """current 1.2.50 / remote 1.2.51 -> notification."""
    author, user = at_1250
    push_version(author, "1.2.51")
    st = updater.status(root=user)
    assert st["available"] is True and st["latest"] == "1.2.51" and st["reason"] is None


def test_apply_never_goes_back(at_1250):
    author, user = at_1250
    push_version(author, "1.2.49")
    before = run(user, "rev-parse", "HEAD")
    with pytest.raises(updater.SharkyClipsError, match="older"):
        updater.apply(root=user)
    assert run(user, "rev-parse", "HEAD") == before
    assert (user / "VERSION").read_text(encoding="utf-8").strip() == "1.2.50"


def test_restart_state_only_for_a_newer_disk_version(tmp_path):
    (tmp_path / "VERSION").write_text("1.2.50\n", encoding="utf-8")
    assert updater.restart_state("1.2.50", root=tmp_path)["restart_needed"] is False
    assert updater.restart_state("1.2.51", root=tmp_path)["restart_needed"] is False  # older files: never
    assert updater.restart_state("1.2.49", root=tmp_path)["restart_needed"] is True
    assert updater.restart_state("1.2.49", root=tmp_path)["disk"] == "1.2.50"


def test_stale_git_lock_is_cleaned(at_1250):
    import os
    import time

    _, user = at_1250
    lock = user / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - updater.STALE_LOCK_S - 60
    os.utime(lock, (old, old))
    assert updater.clean_stale_state(user) == ["index.lock"] and not lock.exists()


def test_committed_ui_build_matches_version():
    """The release flow builds the UI AFTER the version bump: the UI's own version (Shell.tsx
    reads VERSION at build time) must be the one in VERSION, or the app tells users an older
    version is "new" (the 1.2.47 / 1.2.49 bug)."""
    root = Path(__file__).resolve().parents[2]
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    bundles = list((root / "frontend" / "dist" / "assets").glob("index-*.js"))
    if not bundles:
        pytest.skip("no UI build")
    text = "".join(b.read_text(encoding="utf-8", errors="replace") for b in bundles)
    import re

    # `1.2.51<newline>` (template literal) or "1.2.51\n" (escaped) - whatever the minifier wrote
    assert re.search("[`\"']" + re.escape(version) + r"(\\[rn]|\s)*[`\"']", text), \
        f"frontend/dist was built from another VERSION than {version}: run `npm run build` after the bump"
