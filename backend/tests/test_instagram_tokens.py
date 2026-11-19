"""1.2.37: Instagram with either kind of token. IGAA... (Instagram Login) keeps talking to
graph.instagram.com; EAA... (a Facebook-side token, e.g. a business portfolio's system user from
"API setup with unified login") goes to graph.facebook.com: account lookup, Reels upload,
views, delete, no refresh, revocation, and clear errors in the UI language."""

from __future__ import annotations

import time

import pytest

from fake_platforms import FB_IG_ID, FB_TOKEN, IG_TOKEN, FakePlatforms
from sharkyclips import config, platforms as PF
from sharkyclips.errors import SharkyClipsError

pytestmark = pytest.mark.usefixtures("meta_on")  # 1.2.50: the Instagram API engine (off in the product)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakePlatforms()
    for env in ("SHARKYCLIPS_TIKTOK_ENDPOINT", "SHARKYCLIPS_IG_ENDPOINT", "SHARKYCLIPS_FB_ENDPOINT"):
        monkeypatch.setenv(env, f.url)
    monkeypatch.setenv("SHARKYCLIPS_DOWNLOADS", str(tmp_path / "dl"))
    PF.forget("reels")
    old_lang = config.get("ui_language")
    config.update(ui_language="en")
    yield f
    f.close()
    PF.forget("reels")
    config.update(ui_language=old_lang)


def test_pasted_tokens_lose_spaces_line_breaks_and_wrappers():
    assert PF.clean_token("  EAAB\ncdef  ghij\r\n") == "EAABcdefghij"
    assert PF.clean_token('"IGAAxyz"') == "IGAAxyz"
    assert PF.clean_token("Bearer EAAxyz") == "EAAxyz"
    assert PF.clean_token("https://localhost/#access_token=EAAq1w2&expires_in=0") == "EAAq1w2"
    assert PF.token_kind("EAAxyz") == "facebook" and PF.token_kind("IGAAxyz") == "instagram"
    assert PF.token_kind("abc") is None


def test_facebook_token_connects_through_graph_facebook(fake):
    pasted = f"  {FB_TOKEN[:12]}\n{FB_TOKEN[12:]}  \n"  # wrapped over two lines, spaces around
    st = PF.ig_connect(pasted)
    assert st["connected"] and st["account"] == "democreator48"
    tok = PF.load_token("reels")
    assert tok["access_token"] == FB_TOKEN and tok["graph"] == "facebook" and tok["user_id"] == FB_IG_ID
    assert tok["expires_at"] == 0 and tok["token_type"] == "SYSTEM_USER" and "instagram_content_publish" in tok["scope"]
    assert not [c for c in fake.state["calls"] if c[1].startswith("/v21.0/")]  # never the Instagram graph
    a = PF.account("reels")
    assert a["handle"] == "democreator48" and a["health"]["pill"] == "ok" and a["stats"]
    # a system-user token doesn't expire: nothing is refreshed, even long after
    PF.save_token("reels", {**tok, "saved_at": time.time() - 30 * 86400})
    assert PF.keep_fresh() == [] and fake.state["refreshes"] == 0
    PF._ig_token()
    assert fake.state["refreshes"] == 0


def test_facebook_token_finds_the_account_through_the_business_portfolio(fake):
    fake.state["fb_pages"] = "business"  # a system user: the Page comes with the portfolio, not /me/accounts
    assert PF.ig_connect(FB_TOKEN)["account"] == "democreator48"
    assert ("GET", "/v24.0/me/businesses") in fake.state["fb_calls"]


def test_facebook_token_with_the_account_id_given(fake):
    fake.state["fb_pages"] = "none"  # nothing to discover: the id typed in the account field
    with pytest.raises(SharkyClipsError, match="doesn't reach any Instagram"):
        PF.ig_connect(FB_TOKEN)
    assert PF.ig_connect(FB_TOKEN, FB_IG_ID)["account"] == "democreator48"
    fake.state["fb_pages"] = "pages"  # a username or profile link is looked up among the token's accounts
    assert PF.ig_connect(FB_TOKEN, "https://www.instagram.com/democreator48/")["account"] == "democreator48"
    assert PF.ig_connect(FB_TOKEN, "@DemoCreator48")["account"] == "democreator48"


def test_wrong_account_is_said_clearly_in_turkish(fake):
    config.update(ui_language="tr")
    with pytest.raises(SharkyClipsError) as e:
        PF.ig_connect(FB_TOKEN, "@baskabiri")
    assert e.value.message == "Bu token başka bir hesaba ait: @baskabiri hesabına paylaşım yapamaz."
    assert "işletme portföyü" in e.value.hint
    with pytest.raises(SharkyClipsError, match="başka bir hesaba ait: 1999"):
        PF.ig_connect(FB_TOKEN, "1999")  # an id the token can't open
    with pytest.raises(SharkyClipsError, match="başka bir hesaba ait: @baskabiri"):
        PF.ig_connect(IG_TOKEN, "@baskabiri")  # the Instagram Login token too
    with pytest.raises(SharkyClipsError, match="kabul etmedi"):
        PF.ig_connect("EAAnotarealtoken000000000000")
    assert not PF.connected("reels")


def test_token_that_may_not_post_is_refused(fake):
    fake.state["fb_no_publish"] = True
    with pytest.raises(SharkyClipsError, match="isn't allowed to post to it") as e:
        PF.ig_connect(FB_TOKEN)
    assert "@democreator48" in e.value.message and "instagram_content_publish" in e.value.hint


def test_reels_upload_views_and_delete_on_graph_facebook(fake, tmp_path):
    PF.ig_connect(FB_TOKEN)
    video = tmp_path / "r.mp4"
    video.write_bytes(b"\1" * 54_321)
    res = PF.ig_post(video, "Huge play #reels", poll=0.01)
    assert res == {"media_id": "MC1", "status": "published", "url": "https://www.instagram.com/reel/MC1/"}
    calls = fake.state["fb_calls"]
    assert ("POST", f"/v24.0/{FB_IG_ID}/media") in calls and ("POST", f"/v24.0/{FB_IG_ID}/media_publish") in calls
    up = fake.state["ig_uploads"]["C1"]
    assert up["bytes"] == 54_321 and up["form"]["media_type"] == "REELS" and up["form"]["upload_type"] == "resumable"
    assert fake.state["upload_auth"] == [("/ig-api-upload/v24.0/C1", f"OAuth {FB_TOKEN}")]
    assert PF.ig_stats("MC1")["views"] == 321 and ("GET", "/v24.0/MC1/insights") in calls
    assert PF.ig_quota()["used"] == 1
    assert [m["id"] for m in PF.ig_recent()] == ["MC1"]
    assert PF.ig_delete("MC1") and fake.state["ig_deleted"] == ["MC1"] and ("DELETE", "/v24.0/MC1") in calls
    assert not [c for c in fake.state["calls"] if c[1].startswith("/v21.0/")]


def test_big_reels_go_up_in_parts(fake, tmp_path, monkeypatch):
    """1.2.44: rupload answered one 18-19 MB request with 400 ProcessingFailedError (a 1 MB one
    passed); the same bytes as parts (offset + file_size, 206 = part taken) go through."""
    PF.ig_connect(FB_TOKEN)
    monkeypatch.setattr(PF, "IG_CHUNK", 300_000)
    fake.state["ig_max_request"] = 400_000  # a single request with the whole file would be refused
    video = tmp_path / "big.mp4"
    video.write_bytes(bytes(range(256)) * 4000)  # 1,024,000 bytes
    res = PF.ig_post(video, "Big one #reels", poll=0.01)
    assert res["status"] == "published"
    up = fake.state["ig_uploads"]["C1"]
    assert up["bytes"] == 1_024_000 and up["parts"] == [300_000, 300_000, 300_000, 124_000]


def test_a_refused_upload_says_what_instagram_said(fake, tmp_path, monkeypatch):
    PF.ig_connect(FB_TOKEN)
    fake.state["ig_max_request"] = 10
    video = tmp_path / "r.mp4"
    video.write_bytes(b"\1" * 1000)
    with pytest.raises(PF.PlatformError) as e:
        PF.ig_post(video, "x", poll=0.01)
    assert "ProcessingFailedError" in e.value.message  # what Instagram said
    assert e.value.code == PF.IG_PROCESSING_FAILED and e.value.kind == "transient"  # 1.2.46: tried again


def test_instagram_login_token_stays_on_graph_instagram(fake, tmp_path):
    PF.ig_connect(f" {IG_TOKEN}\n")
    assert PF.load_token("reels")["graph"] == "instagram"
    video = tmp_path / "r.mp4"
    video.write_bytes(b"\1" * 1000)
    PF.ig_post(video, "x", poll=0.01)
    assert fake.state["fb_calls"] == [] and fake.state["upload_auth"] == [("/ig-api-upload/v21.0/C1", f"OAuth {IG_TOKEN}")]


def test_revoked_facebook_token_signs_the_account_out(fake, tmp_path):
    PF.ig_connect(FB_TOKEN)
    fake.state["fb_revoked"] = True
    with pytest.raises(PF.PlatformError) as e:
        PF.ig_container("x")
    assert e.value.kind == "auth" and e.value.code == "190"
