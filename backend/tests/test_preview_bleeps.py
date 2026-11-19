"""1.2.45: the live preview bleeps exactly what goes to the platforms (WYSIWYG).

1. One source: the plan the preview gets (studio /plan) carries the render's own bleep ranges
   (bleep_src, source time) and the censored caption text; the bleep editor's choices (exact
   edges per swear, the clip's bleep type, manual bleeps, switches) reach that plan and the
   render through effective_cand (before 1.2.45 they were dropped there).
2. frontend/src/previewAudio.ts (run with Node) builds the same envelope as audiomix.apply_bleeps,
   sample for sample, and maps source -> output time like plan.out_ranges.
3. A real render (the final-preview size): the bleep tone in the file starts / ends where the
   preview schedules it, within a couple of milliseconds.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from sharkyclips import clips, config
from sharkyclips.clips import ClipOptions, OutputSpec
from sharkyclips.edit import audiomix, plan as plan_mod
from sharkyclips.project import Project, write_json_atomic
from sharkyclips.web import state
from test_preview_math import _node_ts

ROOT = Path(__file__).resolve().parents[2]
SR = 48000
SWEARS = [(1.30, 1.70, "siktir"), (3.60, 4.00, "orospu"), (5.00, 5.25, "amk"), (8.00, 8.40, "sikeyim")]
FILLER = [(0.70, 1.10, "kanka"), (1.90, 2.20, "bak"), (4.30, 4.70, "geldi"), (5.50, 5.90, "oğlum"),
          (7.20, 7.60, "abi"), (8.70, 9.00, "tamam")]
CAND = {"id": "c01", "rank": 1, "start": 0.5, "end": 9.8, "duration": 9.3, "peak": 5.0, "text": "x"}
OPTS = {"profanity": "auto", "hook": False, "hook_flash": False, "replay": "off", "subtitles": "bold", "cuts": "off"}


def _speech(n: int, rng) -> np.ndarray:
    """Speech-like noise, 200-3000 Hz, with a hole around the 1 kHz bleep (so the tone can be
    measured to the millisecond in the rendered file)."""
    x = rng.standard_normal(n + 64).astype(np.float32)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec[(f < 200) | (f > 3000) | ((f > 750) & (f < 1300))] = 0
    y = np.fft.irfft(spec, len(x))[:n].astype(np.float32)
    t = np.arange(n) / SR
    y *= (0.55 + 0.45 * np.sin(2 * np.pi * 5 * t)).astype(np.float32)
    return 0.2 * y / (np.sqrt(np.mean(y ** 2)) + 1e-9)


def _project(name: str) -> Project:
    ffmpeg = shutil.which("ffmpeg")
    d = config.workspace_dir() / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    rng = np.random.default_rng(45)
    dur = 10.5
    y = (rng.standard_normal(int(SR * dur)) * 0.002).astype(np.float32)
    for a, b, _w in SWEARS + FILLER:
        y[int(a * SR):int(b * SR)] += _speech(int(b * SR) - int(a * SR), rng)
    with wave.open(str(d / "voice.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={dur}",
                    "-i", str(d / "voice.wav"), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", str(SR), "-shortest", str(d / "source.mp4")],
                   check=True, capture_output=True)
    (d / "voice.wav").unlink()
    write_json_atomic(d / "meta.json", {"title": "preview bleeps", "duration": dur, "width": 640, "height": 360,
                                        "fps": 30.0, "mode": "gaming", "audio_codec": "aac"})
    words = sorted(SWEARS + FILLER)
    write_json_atomic(d / "transcript.json", {"language": "tr", "created_at": "t", "segments": [
        {"start": 0.5, "end": 9.8, "words": [{"start": a, "end": b, "word": " " + w, "probability": 0.95}
                                             for a, b, w in words]}]})
    return Project(name, d)


def _plan_like_the_preview(proj: Project, edits: dict | None = None) -> plan_mod.EditPlan:
    """What studio.api_plan builds for the preview: the saved review edits through effective_cand."""
    review = {"edits": {"c01": edits} if edits else {}, "music": {}, "dub": {}}
    eff = state.effective_cand(CAND, review)
    return plan_mod.build(proj, eff, ClipOptions.from_dict(OPTS), vertical=True, fps=30)


def _run_ts(tmp_path: Path, body: str) -> dict:
    node = _node_ts()
    if not node:
        pytest.skip("needs Node with TypeScript support (22.18+)")
    src = (ROOT / "frontend" / "src" / "previewAudio.ts").as_uri()
    script = tmp_path / "run.mjs"
    script.write_text(f"const m = await import({json.dumps(src)});\n{body}\n", encoding="utf-8")
    res = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


# ------------------------------------------------------------------ 1. one source


def test_bleep_editor_choices_reach_the_plan_and_the_render(tmp_path):
    proj = _project("pv-plan")
    p0 = _plan_like_the_preview(proj)
    d = p0.to_dict()
    assert d["bleep_src"] and d["bleep_fade"] == audiomix.BLEEP_FADE
    assert [tuple(x) for x in d["bleeps"]] == [tuple(round(v, 3) for v in r) for r in p0.out_ranges(p0.bleep_src)]
    # the caption text the render burns in, censored by the server (not re-done in the browser)
    shown = {w["word"].strip().lower(): w.get("shown") for w in d["words"]}
    assert shown["siktir"] and set(shown["siktir"]) <= {"*", "s"} and shown["kanka"] is None
    items = {it["word"].strip().lower(): it for it in p0.swears}
    assert all(it.get("range") for it in items.values() if it["bleep"])

    # switches + manual bleeps (they used to be dropped by effective_cand)
    p1 = _plan_like_the_preview(proj, {"unbleep": [[1.30, 1.70]], "bleep": [[7.20, 7.60]]})
    by = {it["word"].strip().lower(): it for it in p1.swears}
    assert not by["siktir"]["bleep"] and by["abi"]["status"] == "manual"
    assert not any(a < 1.5 < b for a, b in p1.bleep_src) and any(a <= 7.3 and b >= 7.5 for a, b in p1.bleep_src)

    # dragged edges for one detected swear: exactly that range, no padding, never widened
    p2 = _plan_like_the_preview(proj, {"bleep_set": [[5.00, 5.25, 4.95, 5.40]]})
    assert [4.95, 5.4] in [[round(a, 3), round(b, 3)] for a, b in p2.bleep_src]
    assert {it["word"].strip().lower(): it for it in p2.swears}["amk"]["range"] == [4.95, 5.4]

    # the clip's bleep type: silence / sound effect
    assert _plan_like_the_preview(proj, {"bleep_type": "mute"}).bleep_mode == "mute"
    p4 = _plan_like_the_preview(proj, {"bleep_type": "boop"})
    assert p4.bleep_mode == "bleep" and p4.bleep_sound == "boop"

    # saved like that, and a changed bleep makes the rendered clip old
    e = state.clean_edit({"bleep_set": [[5, 5.25, 4.95, 5.4], ["x"]], "bleep_type": "mute", "unbleep": [[1.3, 1.7]]})
    assert e == {"bleep_set": [[5.0, 5.25, 4.95, 5.4]], "bleep_type": "mute", "unbleep": [[1.3, 1.7]]}
    base = state.edit_fingerprint(CAND, {"edits": {}, "music": {}}, OPTS)
    assert state.edit_fingerprint(CAND, {"edits": {"c01": e}, "music": {}}, OPTS) != base
    shutil.rmtree(proj.dir, ignore_errors=True)


# ------------------------------------------------------------------ 2. the preview's envelope = audiomix


RANGES = [[0.1003, 0.4127], [0.5, 0.509], [0.75, 0.7501], [1.0, 1.0204], [1.25, 1.9]]


def test_preview_envelope_is_audiomix_sample_for_sample(tmp_path):
    n = int(2.0 * SR)
    got = _run_ts(tmp_path, f"""
const r = {json.dumps(RANGES)};
const env = new Array({n});
for (let s = 0; s < {n}; s++) env[s] = m.envAtSample(s, r);
const st = m.steps(r).map((x) => [x.a, x.b, x.up.t, x.up.dur, x.down.t]);
console.log(JSON.stringify({{env, st, out: m.outRanges([[10.2, 10.6], [12.4, 13.5]], [[10, 12.5], [13, 20]])}}));
""")
    # audiomix: mute a signal of ones -> 1 - envelope
    ones = np.ones((n, 1), np.float32)
    ref = 1.0 - audiomix.apply_bleeps(ones.copy(), [tuple(r) for r in RANGES], "mute")[:, 0]
    assert np.max(np.abs(np.array(got["env"], np.float32) - ref)) < 1e-5
    # the fades start / end on the render's sample grid
    for (a, b), (ta, tb, _up, dur, down) in zip([r for r in RANGES if audiomix._n(r[1]) - audiomix._n(r[0]) >= 2], got["st"]):
        assert ta == audiomix._n(a) / SR and tb == audiomix._n(b) / SR and abs(down - (tb - dur)) < 1e-9
    p = plan_mod.EditPlan(start=10, end=20, fps=30, keep=[(10, 12.5), (13, 20)])
    assert [[round(x, 6) for x in r] for r in got["out"]] == [[round(x, 6) for x in r]
                                                               for r in p.out_ranges([(10.2, 10.6), (12.4, 13.5)])]


# ------------------------------------------------------------------ 3. the real file


def _decode(path: Path) -> np.ndarray:
    ffmpeg = shutil.which("ffmpeg")
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def _tone_edges(x: np.ndarray, near: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Where the 1 kHz tone crosses half its level, rising and falling, around each expected range."""
    t = np.arange(len(x)) / SR
    i_ = x * np.cos(2 * np.pi * 1000 * t)
    q_ = x * np.sin(2 * np.pi * 1000 * t)
    k = np.ones(48) / 48  # 1 ms, centred: no delay
    amp = 2 * np.sqrt(np.convolve(i_, k, "same") ** 2 + np.convolve(q_, k, "same") ** 2)
    out = []
    for a, b in near:
        ia, ib = int(a * SR), int(b * SR)
        plateau = float(np.median(amp[ia + int(0.02 * SR):ib - int(0.02 * SR)]))
        half = plateau / 2
        # search each edge only right around where it should be (speech right after a bleep would
        # leak into the 1 ms demodulator; a bleep in the wrong place finds no edge at all -> nan)
        def cross(j):
            y0, y1 = amp[j - 1], amp[j]
            return (j - 1 + (half - y0) / (y1 - y0 + 1e-12)) / SR

        w0, w1 = ia - int(0.012 * SR), ia + int(0.015 * SR)
        seg = amp[w0:w1] > half
        rise = cross(w0 + int(np.argmax(seg))) if seg.any() and not seg[0] else float("nan")
        w0, w1 = ib - int(0.015 * SR), ib + int(0.012 * SR)
        seg = amp[w0:w1] > half
        j = w0 + len(seg) - int(np.argmax(seg[::-1]))  # first sample below half after the last above
        fall = cross(j) if seg.any() and not seg[-1] else float("nan")
        out.append((rise, fall))
    return out


@pytest.mark.requires_ffmpeg
def test_rendered_bleeps_land_where_the_preview_plays_them(tmp_path):
    proj = _project("pv-render")
    edits = {"bleep_set": [[5.00, 5.25, 4.97, 5.33]]}  # one dragged by hand too
    review = {"edits": {"c01": edits}, "music": {}, "dub": {}}
    eff = state.effective_cand(CAND, review)
    opts = ClipOptions.from_dict(OPTS)
    p = plan_mod.build(proj, eff, opts, vertical=True, fps=30)
    src = [[a, b] for a, b in p.bleep_src]
    assert len(src) >= 3
    # what the preview schedules (source time) -> output time, computed by previewAudio.ts
    ts = _run_ts(tmp_path, f"""
const st = m.steps({json.dumps(src)});
console.log(JSON.stringify({{st: st.map((x) => [x.up.t, x.up.dur, x.down.t, x.down.dur]),
  out: m.outRanges(st.map((x) => [x.a, x.b]), {json.dumps([list(k) for k in p.keep])})}}));
""")
    # the export / final preview: render_clip at the final-preview size
    out = tmp_path / "final.mp4"
    clips.render_clip(proj, eff, opts, None, out, spec=OutputSpec(width=540, height=960, audio_k=160))
    x = _decode(out)
    edges = _tone_edges(x, [tuple(r) for r in ts["out"]])
    errs = []
    for (up_t, up_d, down_t, down_d), (oa, _ob), (ra, rb) in zip(ts["st"], ts["out"], edges):
        shift = oa - up_t  # source -> output (no cuts inside a bleep here)
        want_rise = up_t + shift + (up_d - 1 / SR) / 2  # half way up the raised-cosine fade
        want_fall = down_t + shift + (down_d - 1 / SR) / 2
        errs += [abs(ra - want_rise), abs(rb - want_fall)]
    print("ranges", ts["out"], "edges", edges)
    print("bleep edge differences (ms):", [round(e * 1000, 2) for e in errs])
    assert not any(np.isnan(errs)) and max(errs) < 0.002, [round(e * 1000, 2) for e in errs]  # ms: the same moments
    shutil.rmtree(proj.dir, ignore_errors=True)
