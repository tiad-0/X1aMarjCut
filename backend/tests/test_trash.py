"""1.2.41 delete + trash: single / several clips and whole projects go to a 30-day trash and come
back with Restore; Empty trash and the 30-day limit delete for good; the original video and the
posts on the platforms are only touched when asked; jobs and plans of deleted things stop;
disk usage and the "Temizlik" helper. 1.2.47: the keep days setting (0 = for good at once),
planned posts on a delete (on the platform: untouched; uploaded later by SharkyClips: cancel or
keep them in a "posts only" rest that goes after its last post)."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from sharkyclips import config, publishing as P, trash as T
from sharkyclips.errors import SharkyClipsError
from sharkyclips.project import write_json_atomic
from sharkyclips.web import state
from test_multipost import _client, _rec, _rendered
from test_publishing import _clean_projects, _project, cfg  # noqa: F401 - fixtures


@pytest.fixture
def tr(cfg, tmp_path):  # noqa: F811
    _clean_projects()
    import shutil

    shutil.rmtree(T.root(), ignore_errors=True)
    for k in list(P.load()):
        P.remove(k)
    yield tmp_path
    _clean_projects()
    shutil.rmtree(T.root(), ignore_errors=True)


def _files(project, cid):
    _rendered(project, cid, size=5000)
    (project.cache_dir / "covers").mkdir(parents=True, exist_ok=True)
    (project.cache_dir / "covers" / f"{cid}_face_tall_abc123.jpg").write_bytes(b"j" * 300)
    (project.cache_dir / "thumbnails").mkdir(parents=True, exist_ok=True)
    (project.cache_dir / "thumbnails" / f"{cid}.jpg").write_bytes(b"t" * 200)


def _cids(project):
    return [c["id"] for c in (state.load_highlights(project) or {}).get("candidates") or []]


def test_delete_one_clip_and_restore_it(tr):
    project = _project("pub-trash-one", n=3)
    for cid in ("c01", "c02"):
        _files(project, cid)
    state.save_review(project, {"accepted": ["c01", "c02"], "rejected": ["c03"]})
    item = T.delete_clips(project.id, ["c02"])
    assert item["kind"] == "clips" and item["clip_ids"] == ["c02"] and item["days_left"] == 30
    assert item["bytes"] >= 5000 + 300 + 200
    assert _cids(project) == ["c01", "c03"]
    assert state.load_review(project)["accepted"] == ["c01"]
    assert "c02" not in state.load_clips(project) and "c01" in state.load_clips(project)
    assert not (project.cache_dir / "covers" / "c02_face_tall_abc123.jpg").exists()
    assert (project.cache_dir / "covers" / "c01_face_tall_abc123.jpg").exists()  # the neighbour is untouched
    assert (project.dir / "source.mp4").exists()
    # in the trash
    s = T.summary()
    assert s["count"] == 1 and s["items"][0]["titles"] and s["bytes"] == item["bytes"]
    # undo
    T.restore(item["id"])
    assert _cids(project) == ["c01", "c02", "c03"]
    assert state.load_review(project)["accepted"] == ["c01", "c02"]
    assert "c02" in state.load_clips(project)
    assert (project.cache_dir / "thumbnails" / "c02.jpg").read_bytes() == b"t" * 200
    assert T.summary()["count"] == 0
    with pytest.raises(SharkyClipsError, match="isn't in the trash"):
        T.restore(item["id"])


def test_several_clips_at_once_and_a_manual_clip(tr):
    from sharkyclips.edit import manual

    project = _project("pub-trash-many", n=3)
    m = manual.add(project, 2.0, 7.0) if hasattr(manual, "add") else None
    ids = _cids(project)
    gone = ["c01", "c03"] + ([m["id"]] if m else [])
    item = T.delete_clips(project.id, gone)
    assert _cids(project) == ["c02"]
    if m:
        assert not any(x["id"] == m["id"] for x in manual.load(project))
        assert m["id"] not in _cids(project)  # it doesn't come back through manual.json
    T.restore(item["id"])
    assert _cids(project) == ids
    if m:
        assert any(x["id"] == m["id"] for x in manual.load(project))
    with pytest.raises(SharkyClipsError, match="No clip"):
        T.delete_clips(project.id, ["c99"])


def test_plans_stop_and_come_back(tr):
    from sharkyclips import platforms as PF, pubqueue as PQ

    project = _project("pub-trash-plans", n=2)
    later = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    item = P.new_item(project.id, "c01", {"title": "T"})
    item["publish_at"] = later
    P.put(item)
    PF.record(project, "c01", "tiktok", status="scheduled", publish_at=later, mode="api")
    PQ.plan_batch([{"id": "x", "source": f"{project.id}:c01", "sha256": "s", "file": "/x.mp4", "title": "T",
                    "platforms": ["instagram"], "mode": "gaming"}], PQ.settings(), batch="b")
    rec = T.delete_clips(project.id, ["c01"])
    assert P.get(P.key_of(project.id, "c01")) is None
    assert _rec(project, "c01", "tiktok")["status"] == "idle"
    assert not [e for e in PQ.load_calendar()["entries"] if e.get("source") == f"{project.id}:c01"]
    T.restore(rec["id"])
    assert P.get(P.key_of(project.id, "c01"))["publish_at"] == later
    assert _rec(project, "c01", "tiktok")["status"] == "scheduled"
    assert [e for e in PQ.load_calendar()["entries"] if e.get("source") == f"{project.id}:c01"]


def test_jobs_of_deleted_things_are_cancelled(tr):
    from sharkyclips.web import app as A

    project = _project("pub-trash-jobs", n=2)
    go = threading.Event()

    def slow(job):
        while not job.cancel_requested and not go.is_set():
            time.sleep(0.02)

    j1 = A.jobs.submit("export", "gpu", project.id, {"clip_id": "c01"}, slow, dedupe=False)
    j2 = A.jobs.submit("export", "net", project.id, {"clip_id": "c02"}, slow, dedupe=False)
    T.delete_clips(project.id, ["c01"])
    assert A.jobs.get(j1.id).status == "cancelled"
    assert A.jobs.get(j2.id).status in ("queued", "running")
    T.delete_project(project.id)
    assert A.jobs.get(j2.id).status == "cancelled"
    go.set()


def test_delete_a_project_keeps_the_original_unless_ticked(tr):
    original = tr / "my-recordings" / "stream.mkv"
    original.parent.mkdir()
    original.write_bytes(b"v" * 4000)
    for with_original in (False, True):
        project = _project(f"pub-trash-proj-{int(with_original)}", n=2)
        meta = project.load_meta()
        meta["source"] = {"type": "file", "value": str(original)}
        project.save_meta(meta)
        info = _client().get(f"/api/projects/{project.id}/delete-info").json()
        assert info["original"]["path"] == str(original) and info["original"]["outside_workspace"]
        r = _client().post(f"/api/projects/{project.id}/delete", json={"original": with_original})
        assert r.status_code == 200, r.text
        item = r.json()["item"]
        assert not project.dir.exists() and project.id not in [p.id for p in __import__("sharkyclips.project", fromlist=["x"]).list_projects()]
        assert original.exists() != with_original
        assert item["original"]["moved"] is with_original
        _client().post(f"/api/trash/{item['id']}/restore")
        assert project.dir.is_dir() and (project.dir / "meta.json").is_file() and original.exists()
        assert original.read_bytes() == b"v" * 4000


def test_posts_are_only_deleted_when_ticked(tr, monkeypatch):
    from sharkyclips import platforms as PF, youtube

    project = _project("pub-trash-posts", n=2)
    state.save_upload(project, "c01", {"video_id": "abcdefghijk", "url": "https://youtu.be/abcdefghijk", "privacy": "public"})
    PF.record(project, "c01", "tiktok", status="published", url="https://www.tiktok.com/@me/video/1", mode="semi")
    gone = []
    monkeypatch.setattr(youtube, "delete_video", lambda vid: gone.append(vid) or True)
    info = _client().get(f"/api/projects/{project.id}/delete-info?clips=c01").json()
    assert {p["platform"] for p in info["posts"]} == {"youtube", "tiktok"}
    a = T.delete_clips(project.id, ["c01"])  # not ticked: nothing on YouTube happens
    assert gone == [] and a["posts"] is None
    T.restore(a["id"])
    b = T.delete_clips(project.id, ["c01"], with_posts=True)
    assert gone == ["abcdefghijk"]
    assert [p["platform"] for p in b["posts"]["by_hand"]] == ["tiktok"]  # TikTok's API can't delete
    assert state.load_uploads(project)["c01"][0]["missing"] is True


def test_empty_trash_needs_the_count_and_old_items_expire(tr):
    project = _project("pub-trash-empty", n=3)
    _files(project, "c01")
    a = T.delete_clips(project.id, ["c01"])
    b = T.delete_clips(project.id, ["c02"])
    c = _client()
    r = c.post("/api/trash/empty", json={"count": 5})
    assert r.status_code == 400 and T.summary()["count"] == 2
    r = c.post("/api/trash/empty", json={"count": 2}).json()
    assert r["deleted"] == 2 and r["bytes"] >= 5000 and r["trash"]["count"] == 0
    assert not (T.root() / a["id"]).exists() and not (T.root() / b["id"]).exists()
    # 30 days
    d = T.delete_clips(project.id, ["c03"])
    rec = T.load(d["id"])
    rec["deleted_at"] = (datetime.now(timezone.utc) - timedelta(days=30, minutes=1)).isoformat()
    write_json_atomic(T.root() / d["id"] / "trash.json", rec)
    assert T.purge_expired() == 1 and T.summary()["count"] == 0
    # one item for good
    project2 = _project("pub-trash-empty2", n=1)
    e = T.delete_project(project2.id)
    assert c.delete(f"/api/trash/{e['id']}").json()["trash"]["count"] == 0


def test_restore_refuses_when_the_place_is_taken(tr):
    project = _project("pub-trash-clash", n=2)
    a = T.delete_project(project.id)
    _project("pub-trash-clash", n=1)  # imported again meanwhile
    with pytest.raises(SharkyClipsError, match="same name"):
        T.restore(a["id"])
    project = _project("pub-trash-clash2", n=2)
    b = T.delete_clips(project.id, ["c02"])
    hl = state.load_highlights(project)
    hl["candidates"].append({**hl["candidates"][0], "id": "c02"})  # clips were found anew
    write_json_atomic(project.dir / "highlights.json", hl)
    with pytest.raises(SharkyClipsError, match="again"):
        T.restore(b["id"])


def test_disk_usage_and_cleanup(tr):
    project = _project("pub-trash-clean", n=3)
    for cid in ("c01", "c03"):
        _files(project, cid)
    state.save_review(project, {"accepted": ["c01"], "rejected": ["c03"]})
    (project.dir / "previews").mkdir()
    (project.dir / "previews" / "01_s100.mp4").write_bytes(b"p" * 7000)
    (project.cache_dir / "frames").mkdir(parents=True, exist_ok=True)
    (project.cache_dir / "frames" / "1280_3.0.jpg").write_bytes(b"f" * 900)
    u = T.usage(project)
    assert u["cleanable"]["rejected"]["bytes"] >= 5000 and u["cleanable"]["previews"]["bytes"] == 7000
    assert u["cleanable"]["caches"]["bytes"] == 900 and u["total"] >= u["source"] + 7000
    d = _client().get("/api/disk").json()
    assert any(r["project_id"] == project.id and r["cleanable_bytes"] == u["cleanable_bytes"] for r in d["projects"])
    r = _client().post(f"/api/projects/{project.id}/cleanup", json={"what": ["rejected", "previews", "caches"]}).json()
    assert r["freed_now"] == 900 and r["trashed"]["bytes"] >= 12000
    assert not (project.dir / "previews").exists() and "c03" not in state.load_clips(project)
    assert "c01" in state.load_clips(project) and _cids(project) == ["c01", "c02", "c03"]  # the clip itself stays
    assert r["usage"]["cleanable_bytes"] == 0
    T.restore(r["trashed"]["id"])
    assert (project.dir / "previews" / "01_s100.mp4").exists() and "c03" in state.load_clips(project)


def test_deleted_projects_are_invisible_everywhere(tr):
    project = _project("pub-trash-hidden", n=2)
    T.delete_project(project.id)
    c = _client()
    ids = [p["id"] for p in c.get("/api/projects").json()]
    assert project.id not in ids
    assert c.get(f"/api/projects/{project.id}").status_code in (400, 404)
    assert not list(config.workspace_dir().glob("*/uploads.json")) or all(
        "_trash" not in str(p) for p in config.workspace_dir().glob("*/uploads.json"))
    assert json.loads((T.root() / T.summary()["items"][0]["id"] / "trash.json").read_text(encoding="utf-8"))["kind"] == "project"


# ===================================================================== 1.2.47


def _later(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_keep_days_setting_and_zero_deletes_for_good(tr):
    c = _client()
    project = _project("pub-trash-days", n=3)
    _files(project, "c01")
    a = T.delete_clips(project.id, ["c02"])
    assert a["days_left"] == 30
    r = c.put("/api/trash/settings", json={"keep_days": 7})
    assert r.status_code == 200 and r.json()["keep_days"] == 7 and r.json()["items"][0]["days_left"] == 7
    assert c.put("/api/trash/settings", json={"keep_days": -1}).status_code == 400
    assert c.put("/api/trash/settings", json={"keep_days": "x"}).status_code == 400
    assert c.get(f"/api/projects/{project.id}/delete-info").json()["keep_days"] == 7
    try:
        c.put("/api/trash/settings", json={"keep_days": 0})
        b = T.delete_clips(project.id, ["c01"])
        assert b["purged"] is True and not (T.root() / b["id"]).exists()
        assert not (project.cache_dir / "covers" / "c01_face_tall_abc123.jpg").exists()
        assert T.purge_expired() == 1 and T.summary()["count"] == 0  # the earlier item is past 0 days too
    finally:
        config.update(trash_keep_days=30)


def _plans(project):
    from sharkyclips import platforms as PF

    _rendered(project, "c01", size=6000)
    _rendered(project, "c02", size=6000)
    PF.record(project, "c01", "reels", status="scheduled", publish_at=_later(), mode="api", caption="hi")
    PF.record(project, "c01", "tiktok", status="scheduled", publish_at=_later(), mode="browser", remote=True)
    item = P.new_item(project.id, "c02", {"title": "T"})
    item.update(publish_at=_later(3), video_id="abcdefghijk")
    P.put(item)


def test_delete_info_tells_pending_posts_apart(tr):
    project = _project("pub-trash-pend", n=3)
    _plans(project)
    info = _client().get(f"/api/projects/{project.id}/delete-info").json()
    rows = {(r["clip_id"], r["platform"]): r for r in info["pending"]}
    assert rows[("c01", "reels")]["on_platform"] is False and rows[("c01", "reels")]["file"].endswith("c01.mp4")
    assert rows[("c01", "tiktok")]["on_platform"] is True and rows[("c02", "youtube")]["on_platform"] is True
    assert info["pending_local"] == 1 and info["pending_on_platform"] == 2
    assert info["keep_possible"] is True and info["keep_needs_export"] == 0 and info["keep_bytes"] == 6000
    # the TikTok Studio post is out there: listed with the posts (to delete by hand)
    assert any(p["platform"] == "tiktok" and p["clip_id"] == "c01" for p in info["posts"])


def test_cancel_leaves_posts_on_the_platform_alone(tr):
    from sharkyclips.project import Project

    project = _project("pub-trash-cancel", n=3)
    _plans(project)
    item = _client().post(f"/api/projects/{project.id}/delete", json={"pending": "cancel"}).json()["item"]
    gone = Project(project.id, T.root() / item["id"] / project.id)
    assert _rec(gone, "c01", "reels")["status"] == "idle"
    assert _rec(gone, "c01", "tiktok")["status"] == "scheduled"  # waits in TikTok Studio: untouched
    assert P.get(P.key_of(project.id, "c02"))["video_id"] == "abcdefghijk"  # planned on YouTube: stays


def test_keep_posts_leaves_a_hidden_rest_that_goes_after_its_last_post(tr):
    from sharkyclips import platforms as PF
    from sharkyclips.web import multipost as M

    project = _project("pub-trash-keep", n=3)
    _plans(project)
    (project.cache_dir / "frames").mkdir(parents=True, exist_ok=True)
    (project.cache_dir / "frames" / "x.jpg").write_bytes(b"f" * 900)
    c = _client()
    r = c.post(f"/api/projects/{project.id}/delete", json={"pending": "keep"})
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert [(k["clip_id"], k["platform"]) for k in item["kept_posts"]] == [("c01", "reels")]
    # the rest: JSON + the kept video only
    assert project.dir.is_dir() and (project.dir / "clips" / "c01.mp4").is_file()
    assert not (project.dir / "source.mp4").exists() and not (project.dir / "clips" / "c02.mp4").exists()
    assert not (project.dir / "cache").exists() and (project.dir / "highlights.json").is_file()
    assert project.posts_only and project.id not in [p["id"] for p in c.get("/api/projects").json()]
    assert project.id not in [p["project_id"] for p in c.get("/api/disk").json()["projects"]]
    rec = _rec(project, "c01", "reels")
    assert rec["file_fixed"] is True and rec["status"] == "scheduled"
    assert M._file_for(project, "c01", "reels", rec, None) == project.dir / "clips" / "c01.mp4"
    assert _rec(project, "c01", "tiktok")["remote"] is True
    # a second delete of the rest with "keep": nothing new happens
    assert T.delete_project(project.id, pending="keep")["id"] == item["id"]
    # still planned: the rest stays
    assert T.finish_rests() == 0 and project.dir.is_dir()
    # the post went out -> the rest joins its trash item
    PF.record(project, "c01", "reels", status="published", media_id="1", url="https://instagram.com/reel/x")
    assert T.finish_rests() == 1 and not project.dir.exists()
    assert T.load(item["id"])["rest_moved"] is True and T.summary()["count"] == 1
    # Restore: the whole project, a normal one again
    T.restore(item["id"])
    assert (project.dir / "source.mp4").is_file() and (project.dir / "clips" / "c02.mp4").is_file()
    assert (project.dir / "cache" / "frames" / "x.jpg").is_file() and not project.posts_only
    assert project.id in [p["id"] for p in c.get("/api/projects").json()]


def test_restore_a_rest_while_its_post_is_still_planned(tr):
    project = _project("pub-trash-keep2", n=3)
    _plans(project)
    item = T.delete_project(project.id, pending="keep")
    T.restore(item["id"])
    assert (project.dir / "source.mp4").is_file() and not project.posts_only
    rec = _rec(project, "c01", "reels")
    assert not rec.get("file_fixed") and rec["status"] == "scheduled"  # follows your edits again


def test_cancel_on_a_rest_deletes_it_all(tr):
    project = _project("pub-trash-keep3", n=3)
    _plans(project)
    item = T.delete_project(project.id, pending="keep")
    T.delete_project(project.id, pending="cancel")
    assert not project.dir.exists() and T.summary()["count"] == 1 and T.load(item["id"])["rest_cancelled"] is True


def test_keep_with_zero_days_frees_the_space_at_once(tr):
    from sharkyclips import platforms as PF

    config.update(trash_keep_days=0)
    try:
        project = _project("pub-trash-keep0", n=3)
        _plans(project)
        item = T.delete_project(project.id, pending="keep")
        assert item["purged"] is True and not (T.root() / item["id"]).exists()
        assert (project.dir / "clips" / "c01.mp4").is_file() and not (project.dir / "source.mp4").exists()
        PF.record(project, "c01", "reels", status="published", media_id="1")
        assert T.finish_rests() == 1 and not project.dir.exists() and T.summary()["count"] == 0
    finally:
        config.update(trash_keep_days=30)


def test_keep_needs_an_export_first_runs_as_a_job(tr, monkeypatch):
    from sharkyclips import platforms as PF
    from sharkyclips.web import app as A, multipost as M

    project = _project("pub-trash-keepx", n=3)
    PF.record(project, "c03", "reels", status="scheduled", publish_at=_later(), mode="api", caption="hi")
    assert _client().get(f"/api/projects/{project.id}/delete-info").json()["keep_needs_export"] == 1

    def fake_export(project_, cid, platform, rec, job):
        f = project_.dir / "exports" / f"{cid}_reels.mp4"
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"e" * 3000)
        return f

    monkeypatch.setattr(M, "_file_for", fake_export)
    r = _client().post(f"/api/projects/{project.id}/delete", json={"pending": "keep"}).json()
    assert r["item"] is None and r["job"]["kind"] == "trash_keep"
    end = time.time() + 20
    while A.jobs.get(r["job"]["id"]).status in ("queued", "running") and time.time() < end:
        time.sleep(0.05)
    assert A.jobs.get(r["job"]["id"]).status == "done", A.jobs.get(r["job"]["id"]).error
    assert (project.dir / "exports" / "c03_reels.mp4").is_file() and not (project.dir / "source.mp4").exists()
    assert _rec(project, "c03", "reels")["file_fixed"] is True


def test_keep_waits_for_an_unfinished_youtube_upload(tr):
    project = _project("pub-trash-keepyt", n=2)
    item = P.new_item(project.id, "c01", {"title": "T"})
    item["publish_at"] = _later()
    P.put(item)
    info = _client().get(f"/api/projects/{project.id}/delete-info").json()
    assert info["pending_local"] == 1 and info["keep_possible"] is False
    r = _client().post(f"/api/projects/{project.id}/delete", json={"pending": "keep"})
    assert r.status_code == 400 and "YouTube" in r.text
    assert project.dir.is_dir() and (project.dir / "source.mp4").exists()


def test_a_held_project_folder_still_goes_to_the_trash(tr, monkeypatch):
    """The folder itself is held open (indexer / virus scanner) but its files aren't: they move one
    by one, an empty folder stays (no project any more), Restore fills it again."""
    from sharkyclips.project import list_projects

    project = _project("pub-trash-held", n=2)
    _files(project, "c01")
    real = T._move

    def held(src, dest, seconds=8.0):
        if src == project.dir:
            raise SharkyClipsError(f"\"{src.name}\" is in use.", None)
        return real(src, dest, seconds)

    monkeypatch.setattr(T, "_move", held)
    monkeypatch.setattr(type(project.dir), "rmdir", lambda self: (_ for _ in ()).throw(PermissionError("held")))
    item = T.delete_project(project.id)
    assert project.dir.is_dir() and not any(project.dir.iterdir())
    assert project.id not in [p.id for p in list_projects()]
    assert (T.root() / item["id"] / project.id / "meta.json").is_file()
    assert (T.root() / item["id"] / project.id / "source.mp4").is_file()
    T.restore(item["id"])
    assert (project.dir / "meta.json").is_file() and (project.dir / "clips" / "c01.mp4").is_file()
    assert project.id in [p.id for p in list_projects()]
