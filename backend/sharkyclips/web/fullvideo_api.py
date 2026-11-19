"""API + jobs of the full video on YouTube (1.2.51, see fullvideo.py) and the Channel style
profile (see channelstyle.py). Included by app.py (before its /api 404)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from .. import channelstyle, fullvideo
from ..errors import SharkyClipsError
from .jobs import Job

router = APIRouter()

NET_RETRY = ("network", "backendError", "internalError", None)  # worth one more try


def _app():
    from . import app as appmod  # lazy: app.py imports this module

    return appmod


# ------------------------------------------------------------------ full video: jobs


def submit_prepare(project_id: str, force: bool = False) -> Job:
    """Write the full video's draft (local LLM) -> the confirm screen."""
    A = _app()

    def run(job: Job):
        A.jobs.update(job, stage="long_writing", progress=None)
        d = fullvideo.prepare(A.get_project(project_id), on_stage=lambda s: A.jobs.update(job, stage=s), force=force)
        return {"status": d.get("status"), "title": (d.get("draft") or {}).get("title")}

    return A.jobs.submit("full_prepare", "gpu", project_id, {"force": force}, run, key=f"full_prepare:{project_id}")


def after_analysis(project_id: str) -> None:
    """The AI analysis is done: a wanted full video gets its draft (once)."""
    A = _app()
    try:
        project = A.get_project(project_id)
    except Exception:  # noqa: BLE001
        return
    d = fullvideo.load(project) or {}
    if d.get("status") == "wanted" and A.llm_available():
        submit_prepare(project_id)


def submit_upload(project_id: str, job_id: str | None = None, attempt: int = 1) -> Job:
    """Resumable upload of the original file, private + publishAt (never at once)."""
    from .. import governor, youtube

    A = _app()
    project = A.get_project(project_id)
    d = fullvideo.load(project) or {}
    draft = d.get("draft")
    if not draft:
        raise SharkyClipsError("Prepare the full video first.")
    if d.get("status") in ("scheduled", "published") and (d.get("upload") or {}).get("video_id"):
        raise SharkyClipsError("This full video is already on YouTube.", (d.get("upload") or {}).get("url"))
    if not youtube.connected():
        raise SharkyClipsError("Connect YouTube first (Settings > YouTube).")
    fullvideo.check_daily_limit(project_id)
    path = fullvideo.source_file(project)
    body = fullvideo.body_for_upload(draft)  # checks the time is ahead again

    def run(job: Job):
        project = A.get_project(project_id)
        cur = fullvideo.load(project) or {}
        if (cur.get("upload") or {}).get("video_id"):
            return {"video_id": cur["upload"]["video_id"], "already": True}
        fullvideo.check_daily_limit(project_id)
        fullvideo.patch(project, status="uploading", error=None)
        A.jobs.update(job, stage="uploading", progress=0.0)
        key = f"full:{project_id}:{path.name}:{path.stat().st_size}"
        retries = int(cur.get("retries") or 0)
        while True:
            try:
                video = youtube.upload(path, body, key, on_progress=lambda f: A.jobs.update(job, progress=f),
                                       checkpoint=governor.checkpoint)
                break
            except SharkyClipsError as exc:
                reason = getattr(exc, "reason", None)
                if retries < 1 and reason in NET_RETRY:  # at most ONE automatic retry (it resumes)
                    retries += 1
                    fullvideo.patch(project, retries=retries)
                    A.jobs.update(job, stage="retrying", progress=None)
                    continue
                fullvideo.patch(project, status="failed", retries=retries,
                                error={"message": exc.message, "hint": exc.hint, "at": fullvideo._iso(fullvideo._now()),
                                       "step": "upload", "reason": reason})
                raise
        if not video.get("id"):
            raise SharkyClipsError("YouTube didn't return a video ID.")
        note = None
        if cur.get("draft", {}).get("use_thumbnail", True) and draft.get("thumbnail"):
            A.jobs.update(job, stage="thumbnail", progress=None)
            try:
                from pathlib import Path

                youtube.set_thumbnail(video["id"], Path(draft["thumbnail"]))
            except SharkyClipsError as exc:  # the video is up; the thumbnail is only a note
                note = exc.message
        out = fullvideo.after_upload(project, video, body, {**draft, "retries": retries})
        if note:
            fullvideo.patch(project, thumbnail_error=note)
        return {"video_id": video["id"], "url": out["upload"]["url"], "publish_at": out["upload"]["publish_at"],
                "locked": out["upload"]["locked"]}

    params = {"title": body["snippet"]["title"], "file": path.name, "size_bytes": path.stat().st_size,
              "publish_at": body["status"].get("publishAt"),
              "project_title": project.load_meta().get("title") or project_id}
    return A.jobs.submit("full_upload", "net", project_id, params, run, key=f"full_upload:{project_id}",
                         persist=True, job_id=job_id, attempt=attempt)


def tick() -> list[str]:
    """Publish loop: released full videos link their Shorts; the weekly channel re-analysis."""
    A = _app()
    acts: list[str] = []
    from .. import youtube

    if youtube.connected():
        for p in A.list_projects():
            try:
                if fullvideo.published_due(p):
                    fullvideo.patch(p, status="published")
                    res = fullvideo.link_clips(p)
                    acts.append(f"full_linked:{p.id}:{res['linked']}")
            except Exception:  # noqa: BLE001 - one project never stops the loop
                continue
    if channelstyle.due():
        channelstyle.note_try()
        submit_channel_style()
        acts.append("channel_style")
    return acts


# ------------------------------------------------------------------ full video: API


@router.get("/api/projects/{project_id}/full-upload")
def api_full_view(project_id: str) -> dict:
    return fullvideo.view(_app()._project(project_id))


@router.post("/api/projects/{project_id}/full-upload/prepare")
def api_full_prepare(project_id: str, body: dict | None = None) -> dict:
    project = _app()._project(project_id)
    d = fullvideo.load(project) or {}
    if d.get("status") in (None, "skipped", "failed", "review", "wanted"):
        fullvideo.patch(project, status="wanted", error=None)
    return submit_prepare(project_id, force=bool((body or {}).get("force"))).to_dict()


@router.put("/api/projects/{project_id}/full-upload")
def api_full_save(project_id: str, body: dict) -> dict:
    project = _app()._project(project_id)
    d = fullvideo.load(project) or {}
    if d.get("status") in ("uploading", "scheduled", "published"):
        raise SharkyClipsError("This full video is already on YouTube: change it in YouTube Studio.")
    if not d.get("draft"):
        raise SharkyClipsError("Prepare the full video first.")
    fullvideo.patch(project, draft=fullvideo.clean_draft(d["draft"], body or {}))
    return fullvideo.view(project)


@router.post("/api/projects/{project_id}/full-upload")
def api_full_upload(project_id: str, body: dict | None = None) -> dict:
    """"Upload" on the confirm screen (the edits come along and are saved first)."""
    project = _app()._project(project_id)
    d = fullvideo.load(project) or {}
    if body and d.get("draft"):
        fullvideo.patch(project, draft=fullvideo.clean_draft(d["draft"], body))
    return submit_upload(project_id).to_dict()


@router.post("/api/projects/{project_id}/full-upload/skip")
def api_full_skip(project_id: str) -> dict:
    project = _app()._project(project_id)
    d = fullvideo.load(project) or {}
    if d.get("status") in ("uploading", "scheduled", "published"):
        raise SharkyClipsError("This full video is already on YouTube.")
    fullvideo.patch(project, status="skipped", why="user")
    return fullvideo.view(project)


@router.get("/api/projects/{project_id}/full-upload/thumbnail.jpg")
def api_full_thumb(project_id: str) -> FileResponse:
    from pathlib import Path

    project = _app()._project(project_id)
    f = ((fullvideo.load(project) or {}).get("draft") or {}).get("thumbnail")
    if not f or not Path(f).is_file() or project.dir.resolve() not in Path(f).resolve().parents:
        raise SharkyClipsError("No thumbnail yet.")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ Channel style


def submit_channel_style() -> Job:
    A = _app()

    def run(job: Job):
        return channelstyle.refresh(on_stage=lambda s: A.jobs.update(job, stage=s, progress=None),
                                    on_progress=lambda f: A.jobs.update(job, progress=f))

    return A.jobs.submit("channel_style", "net", None, {}, run, key="channel_style")


def submit_style_preview() -> Job:
    A = _app()

    def run(job: Job):
        res = channelstyle.preview(on_stage=lambda s: A.jobs.update(job, stage=s, progress=None))
        return {"items": len(res["items"])}

    return A.jobs.submit("style_preview", "gpu", None, {}, run, key="style_preview")


@router.get("/api/channel-style")
def api_channel_style(lang: str | None = None) -> dict:
    return channelstyle.view(lang)


@router.put("/api/channel-style")
def api_channel_style_save(body: dict) -> dict:
    channelstyle.save_edits(body or {})
    return channelstyle.view()


@router.post("/api/channel-style/analyze")
def api_channel_style_analyze(request: Request) -> dict:
    return submit_channel_style().to_dict()


@router.post("/api/channel-style/preview")
def api_channel_style_preview() -> dict:
    if not channelstyle.cached_videos().get("videos"):
        raise SharkyClipsError("Analyse the channel first.")
    return submit_style_preview().to_dict()
