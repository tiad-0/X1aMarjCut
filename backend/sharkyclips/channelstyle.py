"""Channel style profile (1.2.51): how the creator writes, learned from their own YouTube channel.

Read only: channels.list (mine) -> the uploads playlist -> playlistItems.list -> videos.list
(snippet, contentDetails, statistics). Read requests need no audit and cost 1 quota unit each
(a 500-video channel: about 22 units). No page scraping, no browser automation, never a write.

Stored next to My style (<settings>/style/):
  channel_videos.json  what YouTube returned (titles, descriptions, tags, views, likes, ...);
                       YouTube API data: refreshed weekly, deleted after 30 days without a
                       refresh and when YouTube is disconnected (ytdata.purge)
  style_profile.json   the analysis + the creator's own edits ("edits": notes, extra rules,
                       always-hashtags, title case per format); re-analysis keeps the edits
  style_profile.md     the readable summary (the Channel style screen shows it too)
  style_preview.json   "what the app would write" for the last 3 videos, next to the real titles
  recent_titles.json   the app's latest chosen titles (the next ones must not start the same way)

What the analysis gives the writers (titles/style.py, titles/__init__.py, longmeta, fullvideo):
  prompt_block(fmt, kind)  the channel's habits + what works best (top 20 % vs bottom 20 % by
                           views) as rules for the local LLM
  title_rules(fmt, kind)   case (ALL CAPS / mixed), emoji count, punctuation, for the local fixes
  ensure_hashtags(tags)    the always-there hashtags (#democreator) in the creator's spelling
  sync_examples()          the best real titles become My style examples (few-shot, never copied)
Instagram / Meta: nothing here talks to them; Instagram texts come from the same metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

VERSION = 1
SHORT_MAX_S = 60          # Shorts: up to 60 s, or up to 3 min with #shorts
SHORT_TAGGED_MAX_S = 180
WEEK = timedelta(days=7)
MAX_AGE = timedelta(days=30)   # YouTube API Services: refresh or delete within 30 days
SETTLE_DAYS = 3                # newer videos haven't found their viewers yet (left out of the comparison)
KINDS = ("gaming", "study", "music")
FORMATS = ("short", "long")
MAX_CHANNEL_EXAMPLES = 80
RECENT_KEEP = 40
_lock = threading.RLock()

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿]️?(?:‍[\U0001F000-\U0001FAFF☀-➿⬀-⯿]️?)*")
HASHTAG = re.compile(r"(?<![\w#])#[^\W_][\w]*", re.UNICODE)
URL = re.compile(r"https?://\S+")
WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
PAREN_END = re.compile(r"\(([^()]{2,48})\)\s*$")
ISO_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

# Turkish streamer talk the analysis looks for (the creator's tone).
SLANG = ["kanka", "abi", "lan", "ulan", "amman", "valla", "vallaha", "resmen", "efsane", "adam", "adamın",
         "çocuk", "çocuğa", "bak", "hadi", "olm", "moruk", "aga", "kardeşim", "şov", "sekans", "sekansı",
         "salak", "salaklığı", "salaklığından", "yedi", "kesiyor", "sildim", "ezdi", "patlattık", "taşıdım",
         "tilt", "rage", "full", "direkt", "işte", "sitem", "amk", "of", "yaa", "ya"]
STOP = {"ve", "ile", "bir", "bu", "da", "de", "mi", "mı", "mu", "mü", "ne", "ben", "beni", "bana", "çok",
        "için", "ama", "gibi", "the", "and", "of", "to", "a", "in", "o", "şu", "en", "her", "hem", "ki",
        "daha", "kadar", "sonra", "diye", "var", "yok", "olan", "oldu", "ise", "bize", "biz", "sen", "siz"}
STUDY_WORDS = re.compile(r"(?<!\w)(study with me|ders|dersi|çalışma|çalışıyoruz|pomodoro|fizik|kimya|matematik|"
                         r"biyoloji|konu anlatımı|lecture|exam|sınav|yks|tyt|ayt|physics|math)(?!\w)", re.I)
MUSIC_WORDS = re.compile(r"(?<!\w)(şarkı|şarkısı|song|music|müzik|lo-?fi|dream pop|bedroom pop|bpm|"
                         r"ai şarkı|ai song|suno|cover|beat|playlist|#dreampop|#lofi\w*)(?!\w)", re.I)
SOCIAL_LINK = re.compile(r"(kick\.com|instagram\.com|facebook\.com|tiktok\.com|twitch\.tv|x\.com|twitter\.com|"
                         r"discord\.gg|youtube\.com/@)", re.I)

# Human-readable feature names (summary + UI): en / tr.
FEATURES = {
    "caps": ("written in CAPITALS", "BÜYÜK HARFLE yazılmış"),
    "emoji": ("has emojis", "emoji var"),
    "two_emoji": ("2+ emojis", "2+ emoji"),
    "question": ("asks a question (?)", "soru soruyor (?)"),
    "exclaim": ("ends with \"!\"", "\"!\" ile bitiyor"),
    "quote": ("opens with a quote (\"...\")", "alıntıyla açılıyor (\"...\")"),
    "paren": ("(PARENTHESIS) at the end", "sonda (PARANTEZ)"),
    "number": ("has a number (1v9, 55 KILL, 4'TE 4)", "sayı var (1v9, 55 KILL, 4'TE 4)"),
    "game": ("names the game", "oyunun adı geçiyor"),
    "slang": ("street talk (kanka, abi, lan ...)", "argo / samimi dil (kanka, abi, lan ...)"),
    "hashtag": ("hashtags in the title", "başlıkta hashtag"),
    "short_len": ("short title (under 45 characters)", "kısa başlık (45 karakterden az)"),
}


# ------------------------------------------------------------------ storage


def folder() -> Path:
    return config.config_dir() / "style"


def _path(name: str) -> Path:
    return folder() / name


def _read(name: str) -> dict:
    p = _path(name)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


DEFAULT_EDITS = {"enabled": True, "notes": "", "extra_rules": [], "avoid": [], "always_hashtags": [],
                 "case": {"short": "auto", "long": "auto"}}


def _brand_hashtag() -> str:
    """The channel hashtag the creator set (Publish queue / Brand kit), e.g. #democreator."""
    pq = config.get("pubqueue") or {}
    tag = str(pq.get("brand_hashtag") or "").strip()
    if not tag:
        tags = ((config.get("channel_text") or {}).get("hashtags") or [])
        tag = str(tags[0]) if tags else ""
    tag = re.sub(r"[^\w]", "", tag.replace("#", ""), flags=re.UNICODE)
    return f"#{tag}" if tag else ""


def clean_edits(d: dict | None) -> dict:
    d = d if isinstance(d, dict) else {}

    def lines(v, n: int, size: int) -> list[str]:
        if isinstance(v, str):
            v = v.split("\n")
        return [" ".join(str(x).split())[:size] for x in (v or []) if str(x).strip()][:n]

    tags = []
    for t in lines(d.get("always_hashtags"), 6, 40):
        w = re.sub(r"[^\w]", "", t.replace("#", ""), flags=re.UNICODE)
        if w and w.casefold() not in {x.casefold().lstrip("#") for x in tags}:
            tags.append("#" + w)
    case = d.get("case") if isinstance(d.get("case"), dict) else {}
    return {"enabled": d.get("enabled") is not False, "notes": str(d.get("notes") or "").strip()[:2000],
            "extra_rules": lines(d.get("extra_rules"), 12, 200), "avoid": lines(d.get("avoid"), 30, 60),
            "always_hashtags": tags,
            "case": {f: (case.get(f) if case.get(f) in ("auto", "upper", "mixed") else "auto") for f in FORMATS}}


def load() -> dict | None:
    """The analysed profile (None: never analysed)."""
    d = _read("style_profile.json")
    if not d.get("styles"):
        return None
    d["edits"] = clean_edits(d.get("edits"))
    return d


def edits() -> dict:
    p = load()
    e = (p or {}).get("edits") or clean_edits(_read("style_profile.json").get("edits"))
    if not e["always_hashtags"] and _brand_hashtag():
        e = {**e, "always_hashtags": [_brand_hashtag()]}
    return e


def applies(lang: str | None) -> bool:
    """The channel is written in My style's language (Turkish here): other output languages
    keep the generic style."""
    if active() is None:
        return False
    try:
        from .titles import style

        want = style.load().get("lang") or "tr"
    except Exception:  # noqa: BLE001
        want = "tr"
    return not lang or lang == want


def active() -> dict | None:
    """The profile when it is on (the creator can switch it off on the Channel style screen)."""
    p = load()
    return p if p and p["edits"]["enabled"] else None


def save_edits(data: dict) -> dict:
    with _lock:
        d = _read("style_profile.json")
        d["edits"] = clean_edits({**clean_edits(d.get("edits")), **(data or {})})
        _write("style_profile.json", d)
        if d.get("styles"):
            _path("style_profile.md").write_text(summary_md(d, _lang()), encoding="utf-8")
        _bump()
        return d["edits"]


def _lang() -> str:
    return str(config.get("ui_language") or config.get("output_language") or "tr")[:5]


_sig = {"v": 0}


def _bump() -> None:
    _sig["v"] += 1
    try:
        from .titles import style

        style._digest_cache.clear()
    except Exception:  # noqa: BLE001
        pass


def digest(fmt: str = "", kind: str = "gaming") -> str:
    """Part of the metadata cache keys: new analysis / edits -> new titles."""
    p = active()
    if p is None:
        return "-"
    st = p["styles"].get(f"{kind}:{fmt}") or {}
    payload = [VERSION, p.get("analyzed_at"), p["edits"], st.get("n"), fmt, kind]
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]


# ------------------------------------------------------------------ fetching (read only)


def duration_s(iso: str | None) -> int:
    m = ISO_DUR.fullmatch(str(iso or ""))
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


# Games the built-in banks don't know (yet): (id, name, aliases incl. the hashtag spelling).
EXTRA_GAMES = [
    ("spider_man_2", "Spider-Man 2", ["spider-man 2", "spiderman 2", "spider man 2", "spiderman2", "spider-man"]),
    ("battlefield_6", "Battlefield 6", ["battlefield 6", "battlefield6", "bf6"]),
    ("rematch", "Rematch", ["rematch"]),
    ("gta_5", "GTA V", ["gta 5", "gta v", "gta5", "gtav"]),
    ("elden_ring", "Elden Ring", ["elden ring", "eldenring"]),
    ("ea_fc", "EA FC", ["ea fc", "eafc", "fc 25", "fc 26", "fifa"]),
    ("rocket_league", "Rocket League", ["rocket league", "rocketleague"]),
]


def _extra_game(*texts: str) -> tuple[str, str]:
    hay = " ".join(t for t in texts if t).casefold()
    for gid, name, aliases in EXTRA_GAMES:
        for a in aliases:
            if re.search(r"(?<![\w-])" + re.escape(a) + r"(?!\w)", hay) or "#" + a.replace(" ", "") in hay:
                return gid, name
    return "", ""


def _game_of(title: str, description: str, tags: list[str]) -> tuple[str, str]:
    """(game id, game name) from the title first, then description and tags ("" if none)."""
    from .titles import banks, glossary

    try:
        gid, _why = banks.detect(title, {"segments": [{"text": " ".join([description[:1500], *tags])}]})
    except Exception:  # noqa: BLE001
        gid = None
    if gid:
        return gid, banks.get(gid).get("name") or gid
    name = glossary.detect_game(title) or glossary.detect_game(" ".join(tags), description[:600])
    if name:
        return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30], name
    return _extra_game(title, " ".join(tags), description[:1500])


def kind_of(v: dict) -> str:
    text = " ".join([v.get("title") or "", (v.get("description") or "")[:800], " ".join(v.get("tags") or [])])
    cat = str(v.get("category") or "")
    if cat == "10" or len(MUSIC_WORDS.findall(text)) >= 2 or (MUSIC_WORDS.search(v.get("title") or "")
                                                                 and not v.get("game")):
        return "music"
    if cat == "27" or STUDY_WORDS.search(v.get("title") or "") or len(STUDY_WORDS.findall(text)) >= 2:
        return "study"
    return "gaming"


def format_of(v: dict) -> str:
    d = int(v.get("duration_s") or 0)
    tagged = "#short" in " ".join([v.get("title") or "", v.get("description") or ""]).lower()
    if 0 < d <= SHORT_MAX_S or (0 < d <= SHORT_TAGGED_MAX_S and tagged):
        return "short"
    return "long"


def normalize(item: dict) -> dict:
    sn = item.get("snippet") or {}
    st = item.get("statistics") or {}
    cd = item.get("contentDetails") or {}
    v = {"id": item.get("id"), "title": " ".join(str(sn.get("title") or "").split()),
         "description": str(sn.get("description") or "").replace("\r", ""),
         "tags": [str(t) for t in sn.get("tags") or []][:60], "published_at": sn.get("publishedAt"),
         "duration_s": duration_s(cd.get("duration")), "views": int(st.get("viewCount") or 0),
         "likes": int(st.get("likeCount") or 0), "comments": int(st.get("commentCount") or 0),
         "category": str(sn.get("categoryId") or ""),
         "privacy": (item.get("status") or {}).get("privacyStatus") or "public"}
    v["game"], v["game_name"] = _game_of(v["title"], v["description"], v["tags"])
    v["format"] = format_of(v)
    v["kind"] = kind_of(v)
    return v


def fetch(on_progress=None, limit: int = 2000) -> dict:
    """Every video and Short of the connected channel (read-only Data API calls)."""
    from . import youtube
    from .errors import SharkyClipsError

    if not youtube.connected():
        raise SharkyClipsError("Connect YouTube first (Settings > YouTube).",
                               "SharkyClips only reads your channel's titles, descriptions, tags and numbers.")
    ch = youtube._get("channels", {"part": "snippet,contentDetails,statistics", "mine": "true"}).get("items") or []
    tok = (youtube._token() or {}).get("channel") or {}
    it = next((c for c in ch if c.get("id") == tok.get("id")), ch[0] if ch else None)
    if it is None:
        raise SharkyClipsError("This Google account has no YouTube channel yet.")
    uploads = ((it.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    ids: list[str] = []
    page = None
    while uploads and len(ids) < limit:
        params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
        if page:
            params["pageToken"] = page
        r = youtube._get("playlistItems", params)
        ids += [x["contentDetails"]["videoId"] for x in r.get("items") or [] if (x.get("contentDetails") or {}).get("videoId")]
        page = r.get("nextPageToken")
        if on_progress:
            on_progress(min(0.5, len(ids) / max(1, int((it.get("statistics") or {}).get("videoCount") or 1)) / 2))
        if not page:
            break
    videos = []
    for i in range(0, len(ids), 50):
        r = youtube._get("videos", {"part": "snippet,contentDetails,statistics,status", "id": ",".join(ids[i:i + 50])})
        videos += [normalize(x) for x in r.get("items") or []]
        if on_progress:
            on_progress(0.5 + 0.5 * min(1.0, (i + 50) / max(1, len(ids))))
    sn = it.get("snippet") or {}
    data = {"version": VERSION, "fetched_at": _iso(),
            "channel": {"id": it.get("id"), "title": sn.get("title"), "custom_url": sn.get("customUrl"),
                        "videos": int((it.get("statistics") or {}).get("videoCount") or 0)},
            "videos": [v for v in videos if v.get("privacy") != "private" or v.get("views")]}
    _write("channel_videos.json", data)
    return data


def cached_videos() -> dict:
    return _read("channel_videos.json")


# ------------------------------------------------------------------ analysis


def _letters(text: str) -> list[str]:
    return [c for c in text if c.isalpha()]


def _caps_ratio(text: str) -> float:
    body = HASHTAG.sub("", URL.sub("", text))
    letters = _letters(body)
    return sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0


def _ending(title: str) -> str:
    body = PAREN_END.sub("", HASHTAG.sub("", EMOJI.sub("", title))).strip()
    if body.endswith("?!") or body.endswith("!?"):
        return "?!"
    if body.endswith("?"):
        return "?"
    if body.endswith("!"):
        return "!"
    if body.endswith(("...", "…")):
        return "..."
    return "none"


def _lower(text: str) -> str:
    from .titles import lexicon

    return lexicon.lower(text, "tr")


def _words(text: str) -> list[str]:
    return [_lower(w) for w in WORD.findall(HASHTAG.sub(" ", URL.sub(" ", EMOJI.sub(" ", text))))]


def features(v: dict) -> dict:
    t = v["title"]
    words = _words(t)
    em = EMOJI.findall(t)
    gname = _lower(v.get("game_name") or "")
    return {"caps": _caps_ratio(t) >= 0.8, "emoji": bool(em), "two_emoji": len(em) >= 2,
            "question": "?" in t, "exclaim": _ending(t) in ("!", "?!"), "quote": t.lstrip().startswith(('"', "“", "'")),
            "paren": bool(PAREN_END.search(HASHTAG.sub("", EMOJI.sub("", t)).strip())),
            "number": bool(re.search(r"\d", HASHTAG.sub("", t))),
            "game": bool(gname) and any(w in gname.split() for w in words if len(w) > 2),
            "slang": any(w in SLANG for w in words), "hashtag": bool(HASHTAG.search(t)),
            "short_len": len(EMOJI.sub("", t).strip()) < 45}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(len(s) - 1, int(k) + 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def _rate(vs: list[dict], key: str) -> float:
    return round(sum(1 for v in vs if features(v)[key]) / len(vs), 2) if vs else 0.0


def _norm_line(line: str) -> str:
    return " ".join(line.split()).strip()


def _desc_parts(desc: str) -> dict:
    """Body / socials / hashtags of one description."""
    lines = [ln.rstrip() for ln in desc.split("\n")]
    tags_lines = [i for i, ln in enumerate(lines) if ln.strip() and all(w.startswith("#") for w in ln.split())]
    tags = HASHTAG.findall(desc)
    first_tag_line = tags_lines[0] if tags_lines else None
    n = len([ln for ln in lines if ln.strip()])
    pos = "none"
    if tags:
        if first_tag_line is not None and first_tag_line >= len(lines) - 3:
            pos = "end"
        elif first_tag_line is not None and first_tag_line <= 1:
            pos = "start"
        else:
            pos = "inline"
    social = [ln for ln in lines if SOCIAL_LINK.search(ln)]
    label = next((_norm_line(lines[i - 1]) for i in tags_lines if i > 0 and re.match(r"^\s*(hashtag|etiket)", lines[i - 1], re.I)), None)
    paras = [p for p in re.split(r"\n\s*\n", desc.strip()) if p.strip()]
    return {"lines": n, "paragraphs": len(paras), "hashtags": tags, "position": pos, "socials": social,
            "label": label, "length": len(desc.strip()), "paras": paras}


def _common_lines(vs: list[dict], min_share: float = 0.3) -> list[str]:
    """Lines that come back in many descriptions (socials, signature, CTA): the fixed parts."""
    if len(vs) < 2:
        return []
    c: Counter = Counter()
    for v in vs:
        seen = set()
        for ln in v["description"].split("\n"):
            n = _norm_line(ln)
            if len(n) >= 6 and not all(w.startswith("#") for w in n.split()) and n not in seen:
                seen.add(n)
                c[n] += 1
    need = max(2, int(round(len(vs) * min_share)))
    return [ln for ln, k in c.most_common(12) if k >= need]


def _openers_closers(vs: list[dict]) -> tuple[list[str], list[str]]:
    """Recurring phrase endings of the first paragraph ("... ile karşınızdayım!") and sign-offs."""
    ends: Counter = Counter()
    signs: Counter = Counter()
    for v in vs:
        paras = _desc_parts(v["description"])["paras"]
        if not paras:
            continue
        first = _norm_line(paras[0])
        w = first.split()
        if len(w) >= 3:
            ends[" ".join(w[-2:])] += 1
        for p in paras[:3]:
            last = re.split(r"(?<=[.!?])\s+", _norm_line(p))[-1]
            if 2 <= len(last.split()) <= 4:
                signs[last] += 1
    need = max(2, len(vs) // 4)
    return [k for k, n in ends.most_common(3) if n >= need], [k for k, n in signs.most_common(3) if n >= need]


def _patterns(titles: list[str]) -> list[dict]:
    """Phrases (2-4 words) and shapes that come back in several titles."""
    grams: Counter = Counter()
    for t in titles:
        ws = _words(t)
        seen = set()
        for n in (2, 3, 4):
            for i in range(len(ws) - n + 1):
                g = ws[i:i + n]
                if g[0] in STOP or g[-1] in STOP:
                    continue
                k = " ".join(g)
                if k not in seen:
                    seen.add(k)
                    grams[k] += 1
    out = [{"pattern": k, "count": n} for k, n in grams.most_common(40) if n >= 2]
    # keep the longest of overlapping phrases ("saniye sonra" inside "dedikten saniye sonra")
    keep: list[dict] = []
    for g in sorted(out, key=lambda x: (-x["count"], -len(x["pattern"]))):
        if not any(g["pattern"] in k["pattern"] and k["count"] >= g["count"] for k in keep):
            keep.append(g)
    shapes = [
        (r"^[\"“'].+[\"”']", '"QUOTE!" + what happened'),
        (r"dedikten\s+\d+\s+saniye\s+sonra", '"X" DEDİKTEN N SANİYE SONRA...'),
        (r"(?<!\w)nasıl(?!\w)", "... NASIL ...?!"),
        (r"\(b[öo]l[üu]m\s*\d+", "(BÖLÜM N)"),
        (r"\d+\s*v\s*\d+|\d+'?\s*(te|ta|de|da)\s*\d+", "numbers: 1v9 / 4'TE 4"),
        (r"(?<!\w)(çok güzel|sonunda|toplam)(?!\w)", "ÇOK GÜZEL / SONUNDA / TOPLAM ..."),
        (r"\s(&|ile)\s", "X & Y / X İLE Y"),
    ]
    for rx, label in shapes:
        n = sum(1 for t in titles if re.search(rx, _lower(t), re.I))
        if n >= 2:
            keep.append({"pattern": label, "count": n, "shape": True})
    return sorted(keep, key=lambda x: -x["count"])[:14]


def substyle(vs: list[dict]) -> dict:
    titles = [v["title"] for v in vs]
    lens = [len(t) for t in titles]
    caps = _rate(vs, "caps")
    em_counts = [len(EMOJI.findall(t)) for t in titles]
    em_top = Counter(e for t in titles for e in EMOJI.findall(t)).most_common(8)
    ends = Counter(_ending(t) for t in titles)
    words = Counter(w for t in titles for w in set(_words(t)) if w not in STOP and len(w) > 2)
    slang = Counter(w for t in titles for w in set(_words(t)) if w in SLANG)
    parens = Counter(m.group(1).strip() for t in titles
                     if (m := PAREN_END.search(HASHTAG.sub("", EMOJI.sub("", t)).strip())))
    parts = [_desc_parts(v["description"]) for v in vs]
    with_desc = [p for p in parts if p["length"]]
    tag_counts = [len(v["tags"]) for v in vs]
    tag_top = Counter(_norm_line(t).lower() for v in vs for t in v["tags"]).most_common(15)
    hashtags = Counter(h for p in parts for h in dict.fromkeys(p["hashtags"]))
    case_counts = Counter(h.lower() for p in parts for h in p["hashtags"])
    spelled: dict[str, str] = {}
    for h, _n in hashtags.most_common():
        spelled.setdefault(h.lower(), h)
    opener_ends, sign_offs = _openers_closers(vs)
    question_end = sum(1 for p in with_desc if p["paras"] and ("👇" in p["paras"][-1] or "?" in p["paras"][-1]
                                                                or any("?" in x and "👇" in x for x in p["paras"])))
    recent = sorted(vs, key=lambda v: v.get("published_at") or "", reverse=True)[:8]
    return {
        "n": len(vs),
        "title": {
            "length": {"p25": _pct(lens, 0.25), "median": _pct(lens, 0.5), "p75": _pct(lens, 0.75)},
            "words_median": _pct([len(_words(t)) for t in titles], 0.5),
            "caps_rate": caps, "recent_caps_rate": _rate(recent, "caps"),
            "case": "upper" if _rate(recent, "caps") >= 0.6 else "mixed",
            "emoji": {"rate": _rate(vs, "emoji"), "lo": int(_pct(em_counts, 0.25)), "hi": int(round(_pct(em_counts, 0.75))),
                      "median": _pct(em_counts, 0.5), "top": [e for e, _n in em_top]},
            "endings": {k: round(ends.get(k, 0) / len(vs), 2) for k in ("!", "?!", "?", "...", "none")},
            "quote_rate": _rate(vs, "quote"), "paren_rate": _rate(vs, "paren"),
            "paren_examples": [p for p, _n in parens.most_common(6)], "number_rate": _rate(vs, "number"),
            "game_rate": _rate(vs, "game"), "hashtag_rate": _rate(vs, "hashtag"), "slang_rate": _rate(vs, "slang"),
            "slang": [w for w, _n in slang.most_common(10)], "top_words": [w for w, _n in words.most_common(25)],
            "patterns": _patterns(titles),
        },
        "description": {
            "share": round(len(with_desc) / len(vs), 2) if vs else 0,
            "length_median": _pct([p["length"] for p in with_desc], 0.5),
            "lines_median": _pct([p["lines"] for p in with_desc], 0.5),
            "paragraphs_median": _pct([p["paragraphs"] for p in with_desc], 0.5),
            "socials_rate": round(sum(1 for p in with_desc if p["socials"]) / len(with_desc), 2) if with_desc else 0,
            "hashtags_median": _pct([len(p["hashtags"]) for p in with_desc], 0.5),
            "hashtag_position": Counter(p["position"] for p in with_desc).most_common(1)[0][0] if with_desc else "none",
            "hashtag_label": Counter(p["label"] for p in with_desc if p["label"]).most_common(1)[0][0]
            if any(p["label"] for p in with_desc) else None,
            "question_rate": round(question_end / len(with_desc), 2) if with_desc else 0,
            "fixed_lines": _common_lines([v for v in vs if v["description"].strip()]),
            "opener_ends": opener_ends, "sign_offs": sign_offs,
        },
        "tags": {"median": _pct(tag_counts, 0.5), "share": round(sum(1 for n in tag_counts if n) / len(vs), 2) if vs else 0,
                 "top": [t for t, _n in tag_top]},
        "hashtags": {"top": [spelled[h] for h, _n in case_counts.most_common(15)],
                     "always": [spelled[h] for h, n in case_counts.most_common(10)
                                if with_desc and n >= 0.6 * len(with_desc)]},
        "recent_titles": [v["title"] for v in recent[:5]],
    }


def performance(vs: list[dict], now: datetime | None = None) -> dict | None:
    """Top 20 % vs bottom 20 % of one format by views (videos older than 3 days)."""
    now = now or _now()
    pool = [v for v in vs if (t := _parse(v.get("published_at"))) is None or (now - t).days >= SETTLE_DAYS]
    if len(pool) < 5:
        return None
    pool = sorted(pool, key=lambda v: v["views"], reverse=True)
    k = max(1, round(len(pool) * 0.2))
    top, bottom = pool[:k], pool[-k:]
    findings = []
    for key in FEATURES:
        a, b = _rate(top, key), _rate(bottom, key)
        if abs(a - b) >= 0.2:
            findings.append({"feature": key, "top": a, "bottom": b, "verdict": "works" if a > b else "weak"})
    findings.sort(key=lambda f: -abs(f["top"] - f["bottom"]))
    tw = Counter(w for v in top for w in set(_words(v["title"])) if w not in STOP and len(w) > 2)
    bw = Counter(w for v in bottom for w in set(_words(v["title"])) if w not in STOP and len(w) > 2)
    lens_top = [len(v["title"]) for v in top]
    lens_bottom = [len(v["title"]) for v in bottom]
    return {"n": len(pool), "k": k,
            "top": [{"title": v["title"], "views": v["views"], "id": v["id"]} for v in top[:8]],
            "bottom": [{"title": v["title"], "views": v["views"], "id": v["id"]} for v in bottom[:8]],
            "views": {"top_median": _pct([v["views"] for v in top], 0.5),
                      "bottom_median": _pct([v["views"] for v in bottom], 0.5)},
            "length": {"top": _pct(lens_top, 0.5), "bottom": _pct(lens_bottom, 0.5)},
            "findings": findings[:8],
            "top_words": [w for w, n in tw.most_common(12) if n >= 2 and bw.get(w, 0) < n][:8]}


def analyze(data: dict, now: datetime | None = None) -> dict:
    vs = [v for v in data.get("videos") or [] if v.get("title")]
    groups: dict[str, list[dict]] = {}
    for v in vs:
        groups.setdefault(f"{v['kind']}:{v['format']}", []).append(v)
        groups.setdefault(f"all:{v['format']}", []).append(v)
    styles = {k: substyle(g) for k, g in groups.items() if g}
    perf = {f: performance([v for v in vs if v["format"] == f and v["kind"] == "gaming"], now)
            or performance([v for v in vs if v["format"] == f], now) for f in FORMATS}
    games = Counter(v["game_name"] for v in vs if v.get("game_name"))
    return {"version": VERSION, "analyzed_at": _iso(now), "fetched_at": data.get("fetched_at"),
            "channel": data.get("channel") or {},
            "counts": {"total": len(vs), "short": sum(1 for v in vs if v["format"] == "short"),
                       "long": sum(1 for v in vs if v["format"] == "long"),
                       "kinds": dict(Counter(v["kind"] for v in vs)), "games": dict(games.most_common(10))},
            "styles": styles, "performance": {f: p for f, p in perf.items() if p}}


# ------------------------------------------------------------------ the whole refresh


def refresh(on_stage=None, on_progress=None) -> dict:
    """Fetch (read only) -> analyse -> save the profile + summary -> update My style examples."""
    if on_stage:
        on_stage("channel_fetching")
    data = fetch(on_progress=on_progress)
    if on_stage:
        on_stage("channel_analyzing")
    prof = analyze(data)
    with _lock:
        old = _read("style_profile.json")
        prof["edits"] = clean_edits(old.get("edits"))
        if not prof["edits"]["always_hashtags"] and _brand_hashtag():
            prof["edits"]["always_hashtags"] = [_brand_hashtag()]
        _write("style_profile.json", prof)
        _path("style_profile.md").write_text(summary_md(prof, _lang()), encoding="utf-8")
    added = sync_examples(data)
    _bump()
    return {"videos": prof["counts"]["total"], "short": prof["counts"]["short"], "long": prof["counts"]["long"],
            "examples": added, "analyzed_at": prof["analyzed_at"]}


def due(now: datetime | None = None) -> bool:
    """Weekly re-analysis (read only) while YouTube is connected."""
    from . import youtube

    if not youtube.connected():
        return False
    d = _read("style_profile.json")
    if d.get("edits") and d["edits"].get("enabled") is False:
        return False
    t = _parse(d.get("analyzed_at"))
    last_try = float((_read("style_state.json") or {}).get("tried", 0))
    if time.time() - last_try < 6 * 3600:
        return False
    # weekly REfresh: the first analysis is the creator's own choice ("Analyse" on the Channel style screen)
    return t is not None and (now or _now()) - t >= WEEK


def note_try() -> None:
    _write("style_state.json", {"tried": time.time()})


def purge(all_: bool = False, now: datetime | None = None) -> int:
    """YouTube API data retention (ytdata): the fetched videos and their numbers go after 30 days
    without a refresh, or at once when YouTube is disconnected. Returns files removed."""
    now = now or _now()
    n = 0
    raw = _read("channel_videos.json")
    t = _parse(raw.get("fetched_at"))
    if raw and (all_ or t is None or now - t > MAX_AGE):
        _path("channel_videos.json").unlink(missing_ok=True)
        _path("style_preview.json").unlink(missing_ok=True)
        n += 1
    prof = _read("style_profile.json")
    t = _parse(prof.get("fetched_at"))
    if prof.get("performance") and (all_ or t is None or now - t > MAX_AGE):
        prof["performance"] = {}  # view counts and the top / bottom lists are API data
        _write("style_profile.json", prof)
        n += 1
    return n


# ------------------------------------------------------------------ My style examples


def sync_examples(data: dict | None = None) -> int:
    """The channel's best gaming titles (per format and game, then the newest) become My style
    examples (source "channel"); earlier channel / YouTube copies of the same videos are replaced."""
    from .titles import style

    data = data or cached_videos()
    vs = [v for v in data.get("videos") or [] if v.get("kind") == "gaming" and v.get("title")]
    if not vs:
        return 0
    chosen: dict[str, dict] = {}
    by: dict[tuple[str, str], list[dict]] = {}
    for v in vs:
        by.setdefault((v["format"], v.get("game") or ""), []).append(v)
    for (_f, _g), g in by.items():
        for v in sorted(g, key=lambda x: x["views"], reverse=True)[:6]:
            chosen[v["id"]] = v
    for f in FORMATS:
        for v in sorted([x for x in vs if x["format"] == f], key=lambda x: x.get("published_at") or "", reverse=True)[:6]:
            chosen[v["id"]] = v
    pick = sorted(chosen.values(), key=lambda v: v["views"])[-MAX_CHANNEL_EXAMPLES:]
    with style._lock:
        p = style.load()
        titles = {_fold(v["title"]) for v in pick}
        keep = [e for e in p["examples"] if e.get("source") != "channel"
                and not (e.get("source") == "youtube" and _fold(e["title"]) in titles)]
        new = []
        for v in pick:  # ascending views: the newest end of the list = the best (examples_for reads newest first)
            body = re.sub(r"\s*#\S+", "", v["title"]).strip() or v["title"]
            new.append({"id": f"yt{v['id']}"[:20], "format": v["format"], "game": v.get("game") or "", "title": body,
                        "description": v["description"][:4000], "hashtags": HASHTAG.findall(v["description"])[:20],
                        "tags": v["tags"][:25], "source": "channel", "views": v["views"],
                        "added_at": v.get("published_at") or _iso()})
        lang = p.get("lang") or "tr"
        style.save({"examples": keep + new, "lang": lang, "enabled": p["enabled"] if style.exists() else True})
    return len(new)


def _fold(text: str) -> str:
    return re.sub(r"\W+", "", _lower(EMOJI.sub("", HASHTAG.sub("", text or ""))))


# ------------------------------------------------------------------ what the writers use


def _style_for(p: dict, fmt: str, kind: str) -> dict | None:
    st = p["styles"]
    return st.get(f"{kind}:{fmt}") or st.get(f"all:{fmt}") or st.get(f"gaming:{fmt}")


def title_rules(fmt: str, kind: str = "gaming") -> dict:
    """Case and emoji counts for the local fixes / checks. No profile: the My style defaults
    (ALL CAPS, exactly 2 emojis, "!" / "?!")."""
    default = {"case": "upper", "emoji_lo": 2, "emoji_hi": 2, "punct": True, "paren": True}
    p = active()
    if p is None:
        return default
    s = _style_for(p, fmt, kind)
    if not s or s["n"] < 3:
        return default
    t = s["title"]
    case = p["edits"]["case"].get(fmt, "auto")
    case = t["case"] if case == "auto" else case
    lo, hi = max(0, t["emoji"]["lo"]), max(t["emoji"]["lo"], min(3, t["emoji"]["hi"]))
    if case == "upper":
        lo, hi = max(lo, 1), max(hi, 2)
    punct = (t["endings"].get("none", 0) or 0) < 0.35
    return {"case": case, "emoji_lo": lo, "emoji_hi": hi, "punct": punct, "paren": t["paren_rate"] >= 0.25}


def ensure_hashtags(tags: list[str], limit: int = 15) -> list[str]:
    """The always-there hashtags (#democreator) in the creator's spelling, once, kept within `limit`.
    One that is already in the list keeps its place (the channel tag often leads: YouTube shows
    the first three); a missing one is added at the end."""
    p = active()  # no Channel style profile (or switched off): the hashtags stay as they are
    always = p["edits"]["always_hashtags"] if p else []
    if not always:
        return list(tags)
    spell = {a.casefold(): a for a in always}
    out: list[str] = []
    for t in tags:
        t = spell.get(t.casefold(), t)
        if t not in out:
            out.append(t)
    missing = [a for a in always if a not in out]
    keep = [t for t in out if t.casefold() not in spell]
    while len(keep) + len(always) > limit and keep:  # make room: drop from the end, never an always-tag
        keep.pop()
    allowed = set(keep)
    return [t for t in out if t in allowed or t.casefold() in spell] + missing


def recent_titles(fmt: str | None = None, n: int = 10) -> list[str]:
    d = _read("recent_titles.json")
    items = [x for x in d.get("items") or [] if not fmt or x.get("format") == fmt]
    return [x["title"] for x in items[-n:]]


def remember(titles: list[str], fmt: str) -> None:
    """The app's chosen titles: the next clips must not start the same way."""
    titles = [" ".join(str(t).split()) for t in titles if str(t).strip()]
    if not titles:
        return
    with _lock:
        d = _read("recent_titles.json")
        items = [x for x in d.get("items") or [] if x.get("title") not in titles]
        items += [{"title": t, "format": fmt, "at": _iso()} for t in titles]
        _write("recent_titles.json", {"items": items[-RECENT_KEEP:]})


def _opening(title: str) -> str:
    ws = _words(title)
    return " ".join(ws[:2])


def repeats_recent(title: str, fmt: str) -> bool:
    """Starts like one of the last few chosen titles ("KANKA ..." after "KANKA ...")."""
    o = _opening(title)
    return bool(o) and any(_opening(t) == o for t in recent_titles(fmt, 6))


def _pct_s(x: float) -> str:
    return f"{round(x * 100)}%"


def prompt_block(fmt: str, kind: str = "gaming", exclude: set[str] | None = None) -> str:
    """The channel's habits for the local LLM ("" without a profile)."""
    p = active()
    if p is None:
        return ""
    s = _style_for(p, fmt, kind)
    if not s:
        return ""
    t, d = s["title"], s["description"]
    r = title_rules(fmt, kind)
    name = "Shorts" if fmt == "short" else "long videos"
    lines = [f"CHANNEL STYLE (learned from the creator's own {s['n']} {kind} {name}; sound exactly like them, "
             "their tone never changes):"]
    lines.append(f"- title length: usually {int(t['length']['p25'])}-{int(t['length']['p75'])} characters "
                 f"(median {int(t['length']['median'])})")
    if r["case"] == "upper":
        lines.append("- the whole title in CAPITALS (the app capitalizes it)")
    else:
        lines.append("- normal sentence case, like the creator writes; only 1-3 punchy words in CAPITALS for emphasis "
                     "(NASIL, DOUBLE KILL, ÇIK ÇIK ÇIK)")
    em = t["emoji"]
    if em["rate"] >= 0.3:
        lines.append(f"- {r['emoji_lo']}-{r['emoji_hi']} emojis, the creator's favourites: {' '.join(em['top'][:6])}")
    else:
        lines.append("- emojis are rare on this channel: at most one")
    ends = sorted(((k, v) for k, v in t["endings"].items() if k != "none" and v >= 0.1), key=lambda x: -x[1])
    if ends:
        lines.append("- endings the creator uses: " + ", ".join(f'"{k}" ({_pct_s(v)})' for k, v in ends))
    if t["quote_rate"] >= 0.15:
        lines.append(f"- {_pct_s(t['quote_rate'])} open with a real quote in double quotes")
    if t["paren_rate"] >= 0.2 and t["paren_examples"]:
        lines.append("- often a (CONTEXT) at the end, like: " + ", ".join(f"({x})" for x in t["paren_examples"][:4]))
    if t["slang"]:
        lines.append("- the creator's own words / tone (Turkish streamer talk, casual, joking): " + ", ".join(t["slang"][:8]))
    pats = [x["pattern"] for x in t["patterns"] if x.get("shape")][:4]
    if pats:
        lines.append("- title shapes they use (X, Y, N are placeholders: fill them with THIS clip's real words, never write X or N): " + "; ".join(pats))
    perf = (p.get("performance") or {}).get(fmt)
    if perf and perf.get("findings"):
        works = [FEATURES[f["feature"]][0] for f in perf["findings"] if f["verdict"] == "works"][:3]
        weak = [FEATURES[f["feature"]][0] for f in perf["findings"] if f["verdict"] == "weak"][:2]
        if works:
            lines.append("- what gets the most views here (best 20% vs worst 20%): " + "; ".join(works))
        if weak:
            lines.append("- seen more on the weakest videos: " + "; ".join(weak))
        if perf.get("top_words"):
            lines.append("- words of the best titles: " + ", ".join(perf["top_words"][:6]))
    if d["length_median"]:
        lines.append(f"- descriptions: about {int(d['length_median'])} characters, {int(d['paragraphs_median'] or 1)} "
                     f"paragraph(s); {_pct_s(d['question_rate'])} end with a question to the viewers")
    ed = p["edits"]
    for rule in ed["extra_rules"]:
        lines.append(f"- {rule}")
    if ed["avoid"]:
        lines.append("- never use: " + ", ".join(ed["avoid"][:12]))
    if ed["notes"]:
        lines.append(f"- the creator's notes: {ed['notes'][:600]}")
    rec = [x for x in recent_titles(fmt, 6) if not exclude or x not in exclude]
    if rec:
        lines.append("- vary the shape: do NOT start like these recent titles of the app: " + " | ".join(rec[-6:]))
    return "\n".join(lines)


def cover_rules(fmt: str = "short", kind: str = "gaming") -> str:
    """Thumbnail / cover text in the channel's voice (2-4 words, their case)."""
    p = active()
    if p is None:
        return ""
    s = _style_for(p, fmt, kind)
    if not s:
        return ""
    words = ", ".join((s["title"]["slang"] or [])[:5])
    return ("Cover text in the creator's voice: 2-4 words in CAPITALS, the punchline of the title"
            + (f", their words fit ({words})" if words else "") + ", no hashtags.")


# ------------------------------------------------------------------ readable summary


def _feat(key: str, lang: str) -> str:
    return FEATURES[key][1 if lang == "tr" else 0]


def summary(p: dict, lang: str = "tr") -> dict:
    """Short facts for the screen: per kind/format style lines + what works."""
    tr = lang == "tr"
    out = []
    for key in sorted(p["styles"], key=lambda k: (k.startswith("all"), k)):
        if key.startswith("all:"):
            continue
        s = p["styles"][key]
        kind, fmt = key.split(":")
        t, d = s["title"], s["description"]
        facts = []
        facts.append((f"Başlık {int(t['length']['p25'])}-{int(t['length']['p75'])} karakter"
                      if tr else f"Titles {int(t['length']['p25'])}-{int(t['length']['p75'])} characters"))
        facts.append(("BÜYÜK HARF" if t["case"] == "upper" else "cümle düzeni + vurgu için BÜYÜK kelimeler")
                     if tr else ("ALL CAPS" if t["case"] == "upper" else "sentence case + CAPS for emphasis"))
        if t["emoji"]["rate"] >= 0.3:
            facts.append((f"{t['emoji']['lo']}-{t['emoji']['hi']} emoji " if tr else f"{t['emoji']['lo']}-{t['emoji']['hi']} emojis ")
                         + " ".join(t["emoji"]["top"][:5]))
        if t["paren_rate"] >= 0.2:
            facts.append(("sonda (PARANTEZ) " if tr else "(PARENTHESIS) ending ") + _pct_s(t["paren_rate"]))
        if t["quote_rate"] >= 0.15:
            facts.append(('"alıntı" ile açılış ' if tr else '"quote" opening ') + _pct_s(t["quote_rate"]))
        if t["slang"]:
            facts.append(("ton: " if tr else "tone: ") + ", ".join(t["slang"][:6]))
        desc = []
        if d["length_median"]:
            desc.append((f"~{int(d['length_median'])} karakter, {int(d['paragraphs_median'] or 1)} paragraf"
                         if tr else f"~{int(d['length_median'])} characters, {int(d['paragraphs_median'] or 1)} paragraph(s)"))
            if d["socials_rate"] >= 0.3:
                desc.append(("sosyal medya bloğu " if tr else "socials block ") + _pct_s(d["socials_rate"]))
            if d["hashtags_median"]:
                pos = {"end": ("sonda", "at the end"), "start": ("başta", "at the start"),
                       "inline": ("metin içinde", "inline"), "none": ("yok", "none")}[d["hashtag_position"]]
                desc.append((f"{int(d['hashtags_median'])} hashtag, {pos[0]}" if tr
                             else f"{int(d['hashtags_median'])} hashtags, {pos[1]}")
                            + (f" ({d['hashtag_label']})" if d["hashtag_label"] else ""))
            if d["question_rate"] >= 0.3:
                desc.append(("izleyiciye soru + 👇 " if tr else "question to viewers + 👇 ") + _pct_s(d["question_rate"]))
            if d["opener_ends"]:
                desc.append(("açılış: …" if tr else "opener: …") + d["opener_ends"][0])
        out.append({"key": key, "kind": kind, "format": fmt, "n": s["n"], "title": facts, "description": desc,
                    "fixed_lines": d["fixed_lines"][:6], "patterns": [x["pattern"] for x in t["patterns"]][:8],
                    "hashtags": s["hashtags"]["top"][:10], "always": s["hashtags"]["always"],
                    "tags": s["tags"]["top"][:8], "examples": s["recent_titles"][:3]})
    perf = []
    for fmt, pf in (p.get("performance") or {}).items():
        perf.append({"format": fmt, "n": pf["n"], "k": pf["k"], "views": pf["views"], "length": pf["length"],
                     "works": [{"label": _feat(f["feature"], lang), "top": f["top"], "bottom": f["bottom"]}
                               for f in pf["findings"] if f["verdict"] == "works"],
                     "weak": [{"label": _feat(f["feature"], lang), "top": f["top"], "bottom": f["bottom"]}
                              for f in pf["findings"] if f["verdict"] == "weak"],
                     "top_words": pf["top_words"], "top": pf["top"][:5], "bottom": pf["bottom"][:5]})
    return {"styles": out, "performance": perf}


KIND_LABEL = {"gaming": ("Oyun", "Gaming"), "study": ("Ders / çalışma", "Study"), "music": ("AI şarkılar / müzik", "Music")}
FMT_LABEL = {"short": ("Shorts", "Shorts"), "long": ("Uzun video", "Long video")}


def summary_md(p: dict, lang: str = "tr") -> str:
    tr = lang == "tr"
    i = 0 if tr else 1
    s = summary(p, lang)
    ch = (p.get("channel") or {}).get("title") or ""
    c = p["counts"]
    L = [f"# {'Kanal Stil Profili' if tr else 'Channel style profile'}: {ch}", "",
         (f"{c['total']} video ({c['short']} Shorts, {c['long']} uzun) · analiz: {p['analyzed_at'][:10]}"
          if tr else f"{c['total']} videos ({c['short']} Shorts, {c['long']} long) · analysed {p['analyzed_at'][:10]}"), ""]
    for st in s["styles"]:
        L += [f"## {KIND_LABEL[st['kind']][i]} · {FMT_LABEL[st['format']][i]} ({st['n']})", ""]
        L += [f"- **{'Başlık' if tr else 'Title'}:** " + "; ".join(st["title"])]
        if st["description"]:
            L += [f"- **{'Açıklama' if tr else 'Description'}:** " + "; ".join(st["description"])]
        if st["patterns"]:
            L += [f"- **{'Kalıplar' if tr else 'Patterns'}:** " + "; ".join(st["patterns"])]
        if st["hashtags"]:
            L += ["- **Hashtag:** " + " ".join(st["hashtags"])]
        if st["fixed_lines"]:
            L += [f"- **{'Sabit kısımlar' if tr else 'Fixed parts'}:**"] + [f"  - {x}" for x in st["fixed_lines"]]
        if st["examples"]:
            L += [f"- **{'Son başlıklar' if tr else 'Latest titles'}:** " + " · ".join(st["examples"])]
        L.append("")
    if s["performance"]:
        L += [f"## {'Performans (üst %20 / alt %20, izlenme)' if tr else 'Performance (top 20% / bottom 20% by views)'}", ""]
        for pf in s["performance"]:
            L += [f"### {FMT_LABEL[pf['format']][i]} ({pf['n']})"]
            for w in pf["works"]:
                L.append(f"- ✅ {w['label']}: {_pct_s(w['top'])} / {_pct_s(w['bottom'])}")
            for w in pf["weak"]:
                L.append(f"- ⚠️ {w['label']}: {_pct_s(w['top'])} / {_pct_s(w['bottom'])}")
            if pf["top_words"]:
                L.append(f"- {'En iyi başlıkların kelimeleri' if tr else 'Words of the best titles'}: " + ", ".join(pf["top_words"]))
            L.append("")
    e = p.get("edits") or {}
    if e.get("notes") or e.get("extra_rules") or e.get("always_hashtags"):
        L += [f"## {'Senin eklediklerin' if tr else 'Your edits'}", ""]
        if e.get("always_hashtags"):
            L.append(f"- {'Her zaman' if tr else 'Always'}: " + " ".join(e["always_hashtags"]))
        L += [f"- {r}" for r in e.get("extra_rules") or []]
        if e.get("notes"):
            L.append(f"- {e['notes']}")
    return "\n".join(L).strip() + "\n"


# ------------------------------------------------------------------ preview: the last 3 videos


def preview(n: int = 3, on_stage=None) -> dict:
    """What the app writes for the creator's last `n` videos, next to their real titles. The
    video itself is left out of the examples (no copying); the "clip" is its own description."""
    from . import llm
    from .analyze import SYSTEM, _forbidden, game_names, lang_name, too_similar
    from .errors import SharkyClipsError
    from .titles import style

    data = cached_videos()
    vs = sorted([v for v in data.get("videos") or [] if v.get("title")], key=lambda v: v.get("published_at") or "",
                reverse=True)[:n]
    if not vs:
        raise SharkyClipsError("Analyse the channel first.")
    base = style.load()
    lang = base.get("lang") or "tr"
    out = []
    with llm.session(llm.choose_model(ignore_fast=True)) as model:
        for v in vs:
            if on_stage:
                on_stage("style_preview")
            mine = style.clone(base)
            mine["examples"] = [e for e in mine["examples"] if e.get("id") != f"yt{v['id']}"[:20]
                                and _fold(e["title"]) != _fold(v["title"])]
            if not mine["examples"]:
                mine = style.sample()
            body = _plain_description(v["description"]) or ", ".join(v["tags"][:8]) or v.get("game_name") or ""
            what = body[:600]
            ctx = (f"Video: {'a YouTube Short' if v['format'] == 'short' else 'a long YouTube video'}\n"
                   f"Game / topic: {v.get('game_name') or v['kind']}\nWhat the video shows: {what}")
            tw = style.write_titles(model, mine, system=SYSTEM, context=ctx, lang=lang, lang_label=lang_name(lang),
                                    fmt=v["format"], gid=v.get("game") or None, game_name=v.get("game_name") or "",
                                    what_happens=what, facts="", clip_text=body, glossary=None, used_titles=[],
                                    too_similar=too_similar, forbidden=_forbidden(v.get("game") or None),
                                    moment=None, liked=[], strong=False, n_out=3, game_names=game_names(),
                                    kind=v["kind"], remember_choice=False)
            out.append({"id": v["id"], "format": v["format"], "kind": v["kind"], "real": v["title"],
                        "views": v["views"], "published_at": v.get("published_at"),
                        "generated": [t["text"] for t in tw["titles"]]})
    res = {"at": _iso(), "items": out}
    _write("style_preview.json", res)
    return res


def _plain_description(desc: str) -> str:
    lines = []
    for ln in desc.split("\n"):
        s = ln.strip()
        if not s or SOCIAL_LINK.search(s) or all(w.startswith("#") for w in s.split()) or re.match(r"^(hashtag|etiket)", s, re.I):
            continue
        if "abone" in s.lower() and "unutmayın" in s.lower():
            continue
        lines.append(HASHTAG.sub("", s).strip())
    return " ".join(lines).strip()


def saved_preview() -> dict | None:
    d = _read("style_preview.json")
    return d if d.get("items") else None


def view(lang: str | None = None) -> dict:
    """Everything the Channel style screen shows."""
    from . import youtube

    p = load()
    lang = lang or _lang()
    raw = _read("style_profile.json")
    return {"profile": p, "edits": edits() if p else clean_edits(raw.get("edits")), "summary": summary(p, lang) if p else None,
            "summary_md": summary_md(p, lang) if p else "", "preview": saved_preview(),
            "connected": youtube.connected(), "channel": (p or {}).get("channel"),
            "analyzed_at": (p or {}).get("analyzed_at"),
            "next_at": _iso(_parse(p["analyzed_at"]) + WEEK) if p and _parse(p.get("analyzed_at")) else None,
            "files": {"profile": str(_path("style_profile.json")), "summary": str(_path("style_profile.md"))}}
