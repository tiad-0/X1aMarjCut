"""Delete + trash + disk usage API (1.2.41): see trash.py."""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import trash as T
from ..errors import SharkyClipsError
from ..project import get_project

router = APIRouter()


def _flag(body: dict | None, key: str) -> bool:
    return bool((body or {}).get(key) is True)


@router.get("/api/trash")
def api_trash() -> dict:
    return T.summary()


@router.put("/api/trash/settings")
def api_trash_settings(body: dict | None = Body(default=None)) -> dict:
    """1.2.47: {"keep_days": 0..365} (0 = what you delete is deleted for good at once)."""
    T.set_keep_days((body or {}).get("keep_days"))
    return T.summary()


@router.post("/api/trash/{tid}/restore")
def api_restore(tid: str) -> dict:
    return {**T.restore(tid), "trash": T.summary()}


@router.delete("/api/trash/{tid}")
def api_purge(tid: str) -> dict:
    return {"bytes": T.purge(tid), "trash": T.summary()}


@router.post("/api/trash/empty")
def api_empty(body: dict | None = Body(default=None)) -> dict:
    """Deletes everything in the trash for good. The UI sends the count it showed: if the trash
    changed meanwhile, nothing happens (the user confirms what they saw)."""
    cur = T.summary()
    want = (body or {}).get("count")
    if want is None or int(want) != cur["count"]:
        raise SharkyClipsError("The trash changed meanwhile.", "Look at it again, then empty it.")
    return {**T.empty(), "trash": T.summary()}


@router.get("/api/projects/{project_id}/delete-info")
def api_delete_info(project_id: str, clips: str | None = None) -> dict:
    """What a delete would touch: size, the original video, posts on the platforms."""
    project = get_project(project_id)
    cids = [c for c in (clips or "").split(",") if c] or None
    orig = T._original(project)
    info = {"project_id": project.id, "title": project.load_meta().get("title") or project.id,
            "posts": T.posts_of(project, cids), "keep_days": T.keep_days(), **T.pending_summary(project, cids)}
    if cids is None:
        info["bytes"] = T.size_of(project.dir)
        info["original"] = ({"path": str(orig), "bytes": T.size_of(orig),
                             "outside_workspace": not T._inside(orig, T.config.workspace_dir())}
                            if orig is not None and not T._inside(orig, project.dir) else None)
    else:
        info["bytes"] = sum(T.size_of(f) for f in T._clip_files(project, set(cids)))
        info["original"] = None
    return info


@router.post("/api/projects/{project_id}/delete")
def api_delete_project(project_id: str, body: dict | None = Body(default=None)) -> dict:
    """body: original / posts (ticks), pending: "keep" | "cancel" (1.2.47: planned posts that
    aren't on the platform yet). Keeping them needs an export first when a post has no
    up-to-date render: then a job does it all and {"job": ...} comes back instead of the item."""
    pending = (body or {}).get("pending") or "cancel"
    if pending not in ("keep", "cancel"):
        raise SharkyClipsError(f"Unknown choice for the planned posts: {pending}.")
    original, posts = _flag(body, "original"), _flag(body, "posts")
    project = get_project(project_id)
    if pending == "keep" and not project.posts_only:
        info = T.pending_summary(project)
        if info["pending_local"] and not info["keep_possible"]:
            raise SharkyClipsError("A YouTube upload of this project isn't finished yet.",
                                   "Try again once the Library shows it planned on YouTube, or cancel the posts.")
        if info["keep_needs_export"]:
            from . import app as A

            def run(job):
                A.jobs.update(job, stage="exporting", progress=None)
                return {"item": T.delete_project(project.id, original, posts, "keep", job)}

            job = A.jobs.submit("trash_keep", "gpu", None,
                                {"target": project.id, "title": project.load_meta().get("title") or project.id,
                                 "posts": info["pending_local"]},
                                run, key=f"trash_keep:{project.id}")
            return {"item": None, "job": job.to_dict(), "trash": T.summary()}
    item = T.delete_project(project_id, with_original=original, with_posts=posts, pending=pending)
    return {"item": item, "trash": T.summary()}


@router.post("/api/projects/{project_id}/clips/delete")
def api_delete_clips(project_id: str, body: dict | None = Body(default=None)) -> dict:
    cids = [str(c) for c in ((body or {}).get("clip_ids") or [])]
    item = T.delete_clips(project_id, cids, with_posts=_flag(body, "posts"))
    return {"item": item, "trash": T.summary()}


@router.get("/api/disk")
def api_disk() -> dict:
    return T.all_usage()


@router.post("/api/projects/{project_id}/cleanup")
def api_cleanup(project_id: str, body: dict | None = Body(default=None)) -> dict:
    return T.cleanup(project_id, list((body or {}).get("what") or []))
