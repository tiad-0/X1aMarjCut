"""1.2.51: Channel style profile (read-only channel analysis) and the full video on YouTube."""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from fake_youtube import FakeYouTube
from sharkyclips import channelstyle as cs
from sharkyclips import config, fullvideo, youtube
from sharkyclips.errors import SharkyClipsError
from sharkyclips.project import Project, write_json_atomic
from sharkyclips.titles import style

SOCIALS = ("🎮 Canlı Yayınlar & Sosyal Medya:\n🟩 Kick: https://kick.com/democreator\n"
           "📸 Instagram: https://www.instagram.com/democreator")

SHORTS = [  # (title, views) - the creator's sentence-case Shorts with "kanka" / "abi"
    ("6-10 adam beni NASIL kesiyor kanka?! 🤯 Shutdown + Double Kill", 1500),
    ("Demir mi bu el? Abi inanmıyor kanka! 💀", 1610),
    ("Bu kadar şansa mı güvendi? 😱", 1590),
    ("Komut verdim ama Renekton'a ölüm düştü! 💀", 1510),
    ("Mermi bitti, şimdi ne yapacağız?", 380),
    ("Ben arabadayken adam neden koşuyordu kanka?", 120),
    ("Roketi attım ve aniden çok iyi bir durum yarattım", 350),
    ('"KARDEŞİMİ KURTARACAĞIM" DEDİKTEN 1 SANİYE SONRA... 💀😂 #democreator', 260),
]
LONGS = [
    ("DEMİR ELODA AKIL SAĞLIĞIMI KAYBETTİM! VİKTOR İLE 1v9 MAÇ TAŞIDIM! ⚡🤬", 10),
    ("TOPLAM 55 KILL & 44 REVIVE! LOBİYİ TEK BAŞIMIZA TEMİZLEDİK! 🚨💀 (DELTA FORCE)", 7),
    ("HAVADA VE KARADA KATLİAM! HELİKOPTER & UÇAKSAVAR ŞOV! 🚁🚀 (DELTA FORCE)", 21),
    ("ÖFKE PATLAMASI! OLAF & NAUTILUS İLE RAKİBİ TİLT ETTİK! 🪓⚓ (FULL RAGE)", 13),
    ("C1 BÖLGESİNE GİRİŞ YASAK! RAKİP TAKIMI SİLDİM! 🔥💥 (DELTA FORCE)", 3),
    ("MASTER YI VE VEIGAR DUO İLE VADİ'DE SEKANSI PATLATTIK!", 185),
]


def _seed(fake: FakeYouTube) -> None:
    t0 = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
    i = 0
    for title, views in SHORTS:
        fake.videos[f"S{i}"] = {
            "snippet": {"title": title, "categoryId": "20", "publishedAt": (t0 + timedelta(days=i)).isoformat(),
                        "description": "Kanka bu ne ya 😂 Siz olsanız ne yapardınız? 👇\n\nHashtag'ler:\n"
                                       "#leagueoflegends #lol #democreator #gaming #shorts",
                        "tags": ["League of Legends Shorts", "Demo Creatorr Shorts"]},
            "status": {"privacyStatus": "public"}, "statistics": {"viewCount": str(views), "likeCount": "4"},
            "contentDetails": {"duration": "PT25S"}}
        i += 1
    for title, views in LONGS:
        fake.videos[f"L{i}"] = {
            "snippet": {"title": title, "categoryId": "20", "publishedAt": (t0 + timedelta(days=i)).isoformat(),
                        "description": "Delta Force'ta efsane bir maçla karşınızdayım!\n\nMaç anlatımı burada. "
                                       "İyi seyirler!\n\nVideoyu beğenmeyi, kanala abone olmayı ve yorum yapmayı "
                                       f"unutmayın! 🚀\n\n{SOCIALS}\n\nHashtag'ler\n#DeltaForce #DemoCreator #GamingTR",
                        "tags": ["Delta Force Türkçe", "Demo Creatorr Delta Force"]},
            "status": {"privacyStatus": "public"}, "statistics": {"viewCount": str(views), "likeCount": "1"},
            "contentDetails": {"duration": "PT25M10S"}}
        i += 1
    fake.videos["M0"] = {
        "snippet": {"title": "Yarım Kalan Masal — a midnight dream pop journey", "categoryId": "22",
                    "publishedAt": (t0 + timedelta(days=i)).isoformat(),
                    "description": '🎧 "Yarım Kalan Masal"\n\nŞarkı Hakkında: gece.\n🎵 Tür: Dream Pop\n⚡ BPM: 85\n'
                                   "#dreampop #lofiturkce", "tags": []},
        "status": {"privacyStatus": "public"}, "statistics": {"viewCount": "8"},
        "contentDetails": {"duration": "PT2M51S"}}


@pytest.fixture
def yt(monkeypatch):
    fake = FakeYouTube()
    monkeypatch.setenv("SHARKYCLIPS_YT_ENDPOINT", fake.start())
    for name in ("token.enc", "token.json", "client.json", "quota.json", "sessions.json", "locked.json",
                 "unlocked.json", "playlists.json", "sync.json", fullvideo.REGISTRY):
        (youtube.folder() / name).unlink(missing_ok=True)
    saved = {f.name: f.read_bytes() for f in cs.folder().glob("*.json")} if cs.folder().is_dir() else {}
    _clear_channel_files()
    old = {k: config.load().get(k) for k in ("pubqueue", "performance_mode", "full_upload_time", "output_language")}
    config.update(pubqueue={"brand_hashtag": "#democreator", "timezone": "Europe/Budapest"},
                  performance_mode="turbo", output_language="tr")
    youtube.set_client({"installed": {"client_id": "1.apps.googleusercontent.com", "client_secret": "s"}})
    url = youtube.auth_url("http://127.0.0.1:8123/")
    loc = requests.get(url, allow_redirects=False, timeout=10).headers["Location"]
    q = dict(__import__("urllib.parse").parse.parse_qsl(loc.split("?", 1)[1]))
    assert youtube.finish_auth(q["code"], q["state"])["connected"]
    yield fake
    _wait_idle()
    fake.stop()
    youtube._forget_token()
    _clear_channel_files()
    if "profile.json" not in saved:
        (cs.folder() / "profile.json").unlink(missing_ok=True)
    for name, data in saved.items():  # My style (profile.json, glossary.json) as it was
        (cs.folder() / name).write_bytes(data)
    (youtube.folder() / fullvideo.REGISTRY).unlink(missing_ok=True)
    c = config.load()
    for k, v in old.items():
        if v is None:
            c.pop(k, None)
        else:
            c[k] = v
    config.save(c)


CHANNEL_FILES = ("channel_videos.json", "style_profile.json", "style_profile.md", "style_preview.json",
                 "recent_titles.json", "style_state.json")


def _clear_channel_files() -> None:
    for name in CHANNEL_FILES:
        (cs.folder() / name).unlink(missing_ok=True)


def _wait_idle(timeout: float = 60) -> None:
    from sharkyclips.web import app as A

    end = time.time() + timeout
    while any(j.status in ("queued", "running") for j in A.jobs.jobs()) and time.time() < end:
        time.sleep(0.1)


# ===================================================================== Channel style


def test_channel_is_read_only_and_profiled(yt):
    _seed(yt)
    before = len(yt.log)
    res = cs.refresh()
    assert res["videos"] == 15 and res["short"] == 8 and res["long"] == 7
    api_calls = [(m, p) for m, p in yt.log[before:] if p.startswith("/www/")]
    assert api_calls and all(m == "GET" for m, _p in api_calls)  # read only: never a write
    assert {p for _m, p in api_calls} <= {"/www/youtube/v3/channels", "/www/youtube/v3/playlistItems",
                                          "/www/youtube/v3/videos"}
    p = cs.load()
    assert p["counts"]["kinds"]["music"] == 1
    short, long = p["styles"]["gaming:short"], p["styles"]["gaming:long"]
    assert short["title"]["case"] == "mixed" and long["title"]["case"] == "upper"
    assert "kanka" in short["title"]["slang"]
    assert long["title"]["paren_rate"] >= 0.5 and "DELTA FORCE" in long["title"]["paren_examples"]
    assert any("kick.com/democreator" in ln for ln in long["description"]["fixed_lines"])
    assert long["description"]["hashtag_position"] == "end"
    perf = p["performance"]["short"]
    assert perf["k"] >= 1 and perf["top"][0]["views"] >= perf["bottom"][0]["views"]
    md = cs._path("style_profile.md").read_text(encoding="utf-8")
    assert "Kanal Stil Profili" in md and "Shorts" in md and "#democreator" in md
    # the best real titles became My style examples (few-shot, never copied)
    ex = [e for e in style.load()["examples"] if e["source"] == "channel"]
    assert ex and all("#" not in e["title"] for e in ex)


def test_rules_follow_the_channel(yt):
    _seed(yt)
    cs.refresh()
    r = cs.title_rules("short")
    assert r["case"] == "mixed"
    title = style.fix_title("kanka bu adam nasıl kesiyor?! 🤯", "tr", "funny", rules=r)
    assert title.startswith("Kanka bu adam") and not title.isupper()
    assert "write the whole title in CAPITALS" not in style.check_title(title, lang="tr", fmt="short",
                                                                         clip_text="", rules=r)
    assert cs.title_rules("long")["case"] == "upper"
    block = cs.prompt_block("short")
    assert "CHANNEL STYLE" in block and "kanka" in block and "sentence case" in block
    assert "CAPITALS" in cs.prompt_block("long")
    # the creator can force the case
    cs.save_edits({"case": {"short": "upper"}})
    assert cs.title_rules("short")["case"] == "upper"


def test_always_hashtag_in_the_creators_spelling(yt):
    _seed(yt)
    cs.refresh()
    out = cs.ensure_hashtags(["#DeltaForce", "#DemoCreator", "#GamingTR"], 12)
    assert out == ["#DeltaForce", "#democreator", "#GamingTR"]  # its place, the creator's spelling
    assert cs.ensure_hashtags([f"#t{i}" for i in range(20)], 12)[-1] == "#democreator"


def test_variety_and_edits_survive_reanalysis(yt):
    _seed(yt)
    cs.refresh()
    cs.remember(["Kanka bu nasıl kill?! 💀"], "short")
    assert cs.repeats_recent("KANKA BU ne ya", "short") and not cs.repeats_recent("Abi bu ne ya", "short")
    assert "do NOT start like" in cs.prompt_block("short")
    cs.save_edits({"notes": "Küfür yok, samimi ol.", "extra_rules": ["Başlıkta oyun adı geçsin"]})
    cs.refresh()
    e = cs.load()["edits"]
    assert e["notes"] == "Küfür yok, samimi ol." and e["extra_rules"] == ["Başlıkta oyun adı geçsin"]
    assert "Başlıkta oyun adı geçsin" in cs.prompt_block("long")


def test_api_and_disconnect_purges(yt):
    from fastapi.testclient import TestClient

    from sharkyclips.web.app import app

    _seed(yt)
    c = TestClient(app)
    job = c.post("/api/channel-style/analyze").json()
    assert job["kind"] == "channel_style"
    _wait_idle()
    v = c.get("/api/channel-style?lang=tr").json()
    assert v["profile"]["counts"]["total"] == 15 and v["summary"]["styles"] and v["connected"]
    assert c.put("/api/channel-style", json={"notes": "x"}).json()["edits"]["notes"] == "x"
    assert not cs.due()  # analysed just now: next week
    youtube.disconnect()
    assert not cs._path("channel_videos.json").exists()  # API data goes with the sign-in
    assert cs.load()["performance"] == {}


# ===================================================================== full video


def make_project(pid: str, minutes: float = 30.0) -> Project:
    d = config.workspace_dir() / pid
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "source.mp4").write_bytes(b"\0" * 400_000)
    dur = minutes * 60
    write_json_atomic(d / "meta.json", {"title": "Delta Force yayını", "duration": dur, "mode": "gaming"})
    cands = []
    for i in range(4):
        s = 120.0 + i * 300
        cands.append({"id": f"c{i + 1:02d}", "rank": i + 1, "start": s, "end": s + 30, "duration": 30, "peak": s + 10,
                      "publish": {"available": True, "titles": [f"KANKA {i + 1}. KILL GELDİ! 🔥💀 (DELTA FORCE)"],
                                  "cover_text": "EFSANE", "language": "tr"}})
    write_json_atomic(d / "highlights.json", {"created_at": "x", "candidates": cands})
    return Project(pid, d)


def test_next_free_evening_and_always_scheduled(yt, monkeypatch):
    zone = fullvideo.tz()
    now = datetime(2026, 11, 16, 19, 30, tzinfo=zone).astimezone(timezone.utc)
    t = fullvideo.next_free_evening(now)  # 20:00 today is only 30 min ahead: tomorrow
    assert t.astimezone(zone).date().isoformat() == "2026-11-17" and t.astimezone(zone).hour == 20
    early = datetime(2026, 11, 16, 9, 0, tzinfo=zone).astimezone(timezone.utc)
    assert fullvideo.next_free_evening(early).astimezone(zone).day == 16
    fullvideo._register({"project_id": "other", "publish_at": fullvideo._iso(fullvideo.next_free_evening(early))})
    assert fullvideo.next_free_evening(early).astimezone(zone).day == 17  # that evening is taken
    config.update(full_upload_time="21:30")
    assert fullvideo.next_free_evening(early).astimezone(zone).strftime("%H:%M") == "21:30"
    with pytest.raises(SharkyClipsError):
        fullvideo.check_time(fullvideo._iso(datetime.now(timezone.utc) + timedelta(minutes=10)))  # never at once
    with pytest.raises(SharkyClipsError):
        fullvideo.check_time(None)


def test_import_switch(yt):
    p = make_project("full-switch")
    assert fullvideo.request(p, False)["status"] == "skipped"
    (p.dir / fullvideo.FILE).unlink()
    assert fullvideo.request(p)["status"] == "wanted"  # default on
    shutil.rmtree(p.dir, ignore_errors=True)


def test_prepare_builds_the_draft_in_the_channel_layout(yt, monkeypatch):
    from sharkyclips import longmeta

    _seed(yt)
    cs.refresh()
    p = make_project("full-prep")
    monkeypatch.setattr(longmeta, "write", lambda *a, **k: {
        "titles": ["55 KILL ATTIK! LOBİYİ SİLDİK! 🚨💀 (DELTA FORCE)"], "language": "tr", "profile": "my_style",
        "body": "Delta Force'ta efsane bir maçla karşınızdayım!\n\nHikaye. İyi seyirler!\n\nAbone olmayı unutmayın! 🚀",
        "hashtags": ["#DeltaForce", "#DemoCreator", "#GamingTR"], "tags": ["Delta Force Türkçe"]})
    d = fullvideo.prepare(p)
    dr = d["draft"]
    assert d["status"] == "review" and dr["category"] == "20" and dr["made_for_kids"] is False
    assert dr["chapters"][0] == {"t": 0.0, "label": "Giriş"} and len(dr["chapters"]) >= 3
    assert "00:00 Giriş" in dr["description"] and "⏱️ Bölümler:" in dr["description"]
    assert "#democreator" in dr["description"] and "#DemoCreator" not in dr["description"]
    assert "democreator" in dr["tags"]
    t = datetime.fromisoformat(dr["publish_at"].replace("Z", "+00:00"))
    assert t > datetime.now(timezone.utc) + timedelta(minutes=59) and t.astimezone(fullvideo.tz()).hour == 20
    shutil.rmtree(p.dir, ignore_errors=True)


def _draft(p: Project) -> None:
    fullvideo.save(p, {"status": "review", "draft": {
        "title": "55 KILL ATTIK! 🚨💀 (DELTA FORCE)", "description": "Hikaye.\n\n#DeltaForce #democreator",
        "tags": ["Delta Force Türkçe", "democreator"], "category": "20", "made_for_kids": False, "language": "tr",
        "publish_at": fullvideo._iso(fullvideo.next_free_evening(skip_project=p.id))}})


def test_upload_is_scheduled_once_a_day_and_links_the_shorts(yt):
    from fastapi.testclient import TestClient

    from sharkyclips.web import fullvideo_api, state
    from sharkyclips.web.app import app

    c = TestClient(app)
    p = make_project("full-up")
    _draft(p)
    yt.fail_inserts = 1  # the first try fails: ONE automatic retry
    job = c.post(f"/api/projects/{p.id}/full-upload", json={"title": "55 KILL ATTIK! 🚨💀 (DELTA FORCE)"}).json()
    assert job["kind"] == "full_upload", job
    _wait_idle()
    d = fullvideo.load(p)
    assert d["status"] == "scheduled" and d["retries"] == 1, d
    vid = d["upload"]["video_id"]
    st = yt.videos[vid]["status"]
    assert st["privacyStatus"] == "private" and st.get("publishAt")  # scheduled, never public at once
    assert yt.videos[vid]["snippet"]["categoryId"] == "20" and st["selfDeclaredMadeForKids"] is False
    # a second full video the same day: refused
    p2 = make_project("full-up2")
    _draft(p2)
    r = c.post(f"/api/projects/{p2.id}/full-upload", json={})
    assert r.status_code >= 400 and "one full video a day" in r.json()["error"].lower()
    # a Short of this project already on YouTube gets "Tam video: <link>" once the video is out
    yt.videos["SHORT1"] = {"snippet": {"title": "Kanka kill", "categoryId": "20", "description": "Kısa açıklama"},
                           "status": {"privacyStatus": "public"}, "statistics": {"viewCount": "1"}}
    state.save_upload(p, "c01", {"video_id": "SHORT1", "url": "https://youtu.be/SHORT1", "privacy": "public"})
    fullvideo.patch(p, upload={**d["upload"], "publish_at": "2020-01-01T20:00:00Z"})
    acts = fullvideo_api.tick()
    assert any(a.startswith(f"full_linked:{p.id}:1") for a in acts)
    assert yt.videos["SHORT1"]["snippet"]["description"].startswith(f"Tam video: https://youtu.be/{vid}")
    assert fullvideo.load(p)["status"] == "published"
    assert fullvideo.with_link("Açıklama", p, "tr").startswith("Tam video: https://youtu.be/")
    for x in (p, p2):
        shutil.rmtree(x.dir, ignore_errors=True)


def test_upload_gives_up_after_one_retry(yt):
    from sharkyclips.web import fullvideo_api

    p = make_project("full-fail")
    _draft(p)
    yt.fail_inserts = 2
    fullvideo_api.submit_upload(p.id)
    _wait_idle()
    d = fullvideo.load(p)
    assert d["status"] == "failed" and d["retries"] == 1 and d["error"]["step"] == "upload"
    shutil.rmtree(p.dir, ignore_errors=True)


def test_chapters_follow_youtubes_rules():
    assert fullvideo.fmt_ts(0) == "00:00" and fullvideo.fmt_ts(3725) == "1:02:05"
    assert fullvideo.chapters_text([]) == ""
    text = fullvideo.chapters_text([{"t": 0, "label": "Giriş"}, {"t": 95, "label": "İlk kill"}])
    assert text.splitlines() == ["⏱️ Bölümler:", "00:00 Giriş", "01:35 İlk kill"]
    assert fullvideo._short_label('"NASIL ÖLDÜM LAN?!" 💀😂 (DELTA FORCE)') == "NASIL ÖLDÜM LAN"
    assert json.dumps(fullvideo.STATES)


def test_weekly_refresh_only_after_the_first_analysis(yt):
    """The scheduler never starts the first analysis by itself; an analysed profile refreshes weekly."""
    _seed(yt)
    assert not cs.due()  # never analysed: the creator starts it
    cs.refresh()
    assert not cs.due()  # analysed just now
    d = json.loads(cs._path("style_profile.json").read_text(encoding="utf-8"))
    d["analyzed_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    cs._write("style_profile.json", d)
    assert cs.due()
    cs.note_try()
    assert not cs.due()  # tried a moment ago: not again for 6 hours
