# SharkyClips security audit (1.2.52, 2026-11-18)

Scope: the whole repository and its full git history (119 commits), the local web server and its
~200 API routes, the integrations (YouTube, Instagram, TikTok, Telegram), the updater, the
installers, CI and the repository settings. Method: code review, secret scanners over the full
history, `pip-audit` / `npm audit`, a regression test for every fix
(`backend/tests/test_security.py`, 98 tests) and a live attack run against a running server.

Severity: **Critical** (remote takeover with no user action beyond visiting a page) ·
**High** · **Medium** · **Low** · **Info**.

## Findings

| ID | Severity | Finding | Location | Fix | Status |
|----|----------|---------|----------|-----|--------|
| W-01 | **Critical** | Any website open in the user's browser could drive the whole API (CSRF): the server trusted every request whose peer was `127.0.0.1`, so a page on evil.example could POST to `http://127.0.0.1:8000` to publish / delete posts, change settings, disconnect accounts, start TikTok automation, run the updater or quit. Body-less endpoints (`/api/quit`, `/api/update/restart`, deletes) worked even as "simple" requests. | `web/app.py` (`_is_local`), every router | New outermost middleware `web/security.py`: every POST / PUT / PATCH / DELETE to `/api/` needs the per-install token `X-SharkyClips-Token` (in `index.html` as a `<meta>`, unreadable to other sites); writes with a foreign `Origin` / `Sec-Fetch-Site: cross-site\|same-site` are refused even with the token. Tray / launcher read the token from `~/.sharkyclips/api_token`. | Fixed |
| W-02 | **High** | DNS rebinding: a domain that resolves to 127.0.0.1 would let a site read every API answer (settings, projects, clip videos) and the UI. | same | `Host` allowlist: localhost / `*.localhost`, IP literals, this computer's name (+ `.local/.lan/.home`); else **421**. | Fixed |
| W-03 | **Medium** | Cross-site reads: other sites could embed clip videos / images / JSON (`<img>`, `<video>`, no-cors fetch). | media routes | GET `/api/...` with `Sec-Fetch-Site: cross-site` refused (only OAuth callback navigations allowed); `Cross-Origin-Resource-Policy: same-origin` on all API answers. | Fixed |
| W-04 | **Medium** | No security headers on the UI: no CSP, framable (clickjacking), no `nosniff`, referrer leaks. | `web/app.py` (`spa`) | CSP (`script-src 'self'` + hashes of the two inline boot scripts, `frame-ancestors 'none'`, `object-src 'none'`, `base-uri`, `form-action`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `COOP`, `Permissions-Policy`; JSON answers get `default-src 'none'; sandbox`. Any `Access-Control-*` header is stripped. | Fixed |
| W-05 | **Medium** | `--lan` without a PIN served the full API (and the media routes) to every device on the network. | `web/auth.py` | Other devices get **403 `pin_required`** for every `/api/` path (media included) until a PIN is set; the UI shows "set a PIN on the PC". | Fixed |
| W-06 | Low | `/api/docs` and `/openapi.json` mapped every endpoint for anyone who could reach the server. | `web/app.py` | Off unless `SHARKYCLIPS_API_DOCS=1`. | Fixed |
| W-07 | Low | No request-size limit: a huge JSON / image body was read into memory. | all JSON / image routes | 64 MB cap in the middleware (Content-Length and chunked); streamed file uploads keep their own limits; the video upload stops while 2 GB disk are still free. | Fixed |
| W-08 | Info | LAN session cookie was not `Secure` behind an https reverse proxy. | `web/auth.py` | `Secure` when the request is https. (`HttpOnly`, `SameSite=Strict`, scrypt hash, rate limit + lockout were already in place.) | Fixed |
| S-01 | **High** | The built-in Google OAuth client **secret** (`GOCSPX-…`) was committed (`backend/sharkyclips/google_client.json`, first in commit `53bbe91`) and is in the public history. GitHub secret scanning confirms it (alerts #1, #2). Anyone can make apps that impersonate "SharkyClips" in Google's consent screen and burn the project's quota. | repo + history | File removed and git-ignored; `google_client.example.json` without secrets; the built-in client is read only from `~/.sharkyclips/google_client.json` (owner's PC). Public builds ship no client: the YouTube card shows the guided "Connect your own Google project" setup. "Reload Google client" re-reads a rotated JSON. History was **not** rewritten (destructive, and the secret must be rotated anyway). | **Owner action: rotate the secret** |
| S-02 | Info | gitleaks false positives: i18n strings ("Enable the YouTube API"), SHA-256 model checksums, Twitch's public web client id (`kimne78…`, sent by every twitch.tv page). | locales, `voices/align.py`, `chat.py` | Reviewed; `.gitleaks.toml` / `.gitleaksignore`. | Not a vulnerability |
| S-03 | Low | Personal data in code and defaults: the owner's handle in every user's TikTok/Reels preview mock (`@utkualsirr`), as a placeholder and a UI hint, and the owner's name / handles in test fixtures; the owner's e-mail in `docs/EXAM_WEEK_LOG.md`. | `PreviewExtras.tsx`, `ChannelStyle.tsx`, `web/live.py`, tests, docs | Neutral placeholders (`@yourchannel`, `democreator`); e-mail removed. Time zone / account / hashtag defaults were already empty (1.2.40). Repository URLs stay (public project). | Fixed (history keeps the old text; commit author e-mail is public) |
| S-04 | Low | Nothing masked token-like text in logs, the console, job errors or API error answers (a library error could print a URL with `access_token=`). | `cli.py`, `web/jobs.py`, `web/app.py` | `redact.py`: Google / Telegram / Meta / GitHub tokens, JWTs, `Bearer …`, `key=value` and JSON secret fields are masked in stdout / stderr (incl. the pythonw log file), the logging module, job errors and every error answer. | Fixed |
| S-05 | Info | Secrets at rest: YouTube, Instagram, TikTok and Telegram tokens and Meta app secrets are encrypted (`secretstore.py`: DPAPI / Keychain / 0600 key file) in `~/.sharkyclips`; LAN PIN only as scrypt hash; no secret is returned by any GET route (tested route by route); exports contain only media and metadata; there is no crash-report upload. The TikTok browser profile (cookies) lives in `~/.sharkyclips/browser/tiktok-profile`, protected by Chrome's own DPAPI cookie encryption. | — | Verified + tests | OK |
| D-01 | Low | Path traversal: all file / media / export / trash / music / brand routes resolve names through registries, regexes or `relative_to` checks. A fuzz over every GET route with `..`, `..\`, absolute and UNC paths found no file read outside the workspace. | all file routes | Regression fuzz test. | OK (verified) |
| D-02 | Low | `open_folder` used `ShellExecute("open")`: given a file instead of a folder it would **run** it. All callers pass app-built folders today. | `easy.py` | Refuses anything that isn't a real, non-symlink, non-UNC folder. | Fixed |
| D-03 | Low | Command injection: no `shell=True`, `os.system`, `eval` or `pickle` anywhere (test enforces it); ffmpeg / yt-dlp / git / ollama get argument lists; user text goes to ffmpeg only through `.ass` files, never into the filter graph. One PowerShell script pasted install paths into a single-quoted string (a `'` in a folder name could break out). | `cli.py` (`_migrate_windows_shortcuts`) | Paths passed as environment variables. | Fixed |
| D-04 | **Medium** | SSRF: "paste a link" handed any http(s) URL to yt-dlp, so a link could make the app fetch `127.0.0.1:…`, the router, a NAS or `169.254.169.254`. | `ingest.py`, `web/app.py`, `web/easy.py` | `netguard.py`: http(s) only, no credentials in the URL, every resolved address must be public (loopback / private / link-local / CGNAT / multicast / reserved / IPv4-mapped refused), checked again on the address yt-dlp ended on. `import_allow_private` setting for a NAS on purpose. | Fixed |
| D-05 | Info | XSS: React escapes all text; no `dangerouslySetInnerHTML` / `innerHTML`; the two server-made HTML pages escape their values; the CSP now also blocks inline / `javascript:` script execution. | UI | Verified | OK |
| D-06 | Info | Parsing: JSON only (`read_json`), no pickle / YAML / eval on untrusted data; tar archives are extracted by file name only. | — | Verified | OK |
| E-01 | Low | Instagram OAuth callback accepted a code **without** `state` (login CSRF: an attacker's account could be connected). | `platforms.py` `ig_finish`, `web/multipost.py` | The browser callback requires `state` (constant-time compare). A code pasted by hand on this PC may still come alone. Meta automation is off since 1.2.50 anyway. | Fixed |
| E-02 | Info | YouTube / TikTok OAuth: `state` + PKCE S256, one-shot listener on `127.0.0.1`, tokens only in POST bodies, `Referrer-Policy: no-referrer` on the callback page. | `youtube.py`, `ytconnect.py`, `platforms.py` | Verified + test | OK |
| E-03 | Low | Telegram: buttons already only work from the paired chat (chat **and** sender id), cards are one-shot (replay), offsets advance. The pairing code was only 32 bits and a group chat could pair. | `phone.py` | 128-bit pairing code; private chats only. | Fixed |
| E-04 | Info | TikTok automation can no longer be started by a web page (W-01); CAPTCHA / verification / login-wall stop rule unchanged and tested. | `tiktok_browser.py` | — | OK |
| F-01 | **Medium** | Updater pulled whatever `origin`'s upstream pointed at (any remote, any branch, any commit after the version check). | `updater.py` | Only `github.com/utkualsir/sharkyclips`, branch `main`; only the commit of tag `v<VERSION>` on that branch, applied with `git merge --ff-only <sha>` (what was checked is what's installed); optional `update_require_signed` (`git verify-tag`). | Fixed |
| F-02 | Low | Laughter model download had no checksum. | `highlights/signals/laughter.py` | SHA-256 of the archive and of both extracted files (cross-checked against an earlier independent download). Other downloads already checked (`downloads.py`); Hugging Face / Ollama verify their own blobs. | Fixed |
| F-03 | Medium | `opentelemetry-api` 1.45.1 (CVE-2026-126360, via FastAPI). | `requirements.txt` | `>=1.45.2`. | Fixed |
| F-04 | Medium | vite 5.4.11 / esbuild 0.21: the **dev server** (`npm run dev`) could be read by any website (GHSA-67mh-4wv8-2f99). Not shipped (the app serves the static build). | `frontend/package.json` | vite 6.4.6 (esbuild 0.25). | Fixed |
| F-05 | Low | `picomatch` ReDoS (GHSA-fc5q-39xc-vjpf), all versions incl. the latest 4.0.7, via vite's file globbing at **build time** only (our own globs). | build tooling | No fixed version exists; Dependabot will offer it. | Accepted (no fix available) |
| F-06 | Low | GitHub Actions used tag references (`@v4`), checkout kept the token in `.git/config`, no secret scanning. | `.github/workflows/ci.yml` | All actions pinned to full commit SHAs, `persist-credentials: false`, `permissions: contents: read`, concurrency; new jobs "Secrets (gitleaks, full history, checksum-verified binary)" and "Dependency audit" (pip-audit + npm audit). | Fixed |
| F-07 | Low | Repository: Dependabot, secret scanning, push protection and private vulnerability reporting were all off (SECURITY.md already pointed to private reporting). | GitHub settings | Enabled via the API: Dependabot alerts + security updates, `.github/dependabot.yml` (pip, npm, actions), secret scanning, push protection, private vulnerability reporting. (Non-provider patterns / validity checks need GitHub Advanced Security.) | Fixed |
| F-08 | Info | Installers: Windows uses `winget` (signed, hash-checked packages) and `git clone` over https; macOS uses the official Homebrew installer and `brew`. No raw binary is downloaded without verification. pip installs from PyPI without hash pinning. | `installer/`, `install.sh` | — | OK / residual (see below) |
| A-01 | Info | YouTube quota: uploads already waited for the reset in the Library; the direct upload dialog just refused at 0. | `youtube.py`, `web/app.py`, `web/publishing.py` | 300-unit reserve (`youtube_quota_reserve`) so uploads stop **before** the limit; an upload over the limit is queued (`youtube/deferred.json`) and started by the scheduler after Google's reset; Settings shows the use. | Done |

## Scanner results

- **gitleaks 8.30.1** (checksum-verified), full history, 119 commits: 327 raw hits → 317 in minified
  `frontend/dist` bundles (i18n strings), 9 i18n / checksum / public-id false positives, **1 real
  secret (S-01)**. Working tree: same false positives; `.venv` third-party test keys ignored.
- **GitHub secret scanning** (enabled during the audit): 2 open alerts, both S-01.
- **pip-audit**: 1 (F-03). **npm audit**: esbuild/vite (F-04, fixed), picomatch (F-05).

## Live attack run (item H26)

Against a running 1.2.52 server (`attacks.py`, fixture workspace):

| Attack | Result |
|---|---|
| `PUT /api/settings` with `Origin: http://evil.example` | 403 `cross_origin` |
| HTML-form `POST /api/quit` (text/plain, cross-site) | 403 `cross_origin` |
| `POST /api/update/restart`, `DELETE /api/projects/…`, TikTok post, YouTube disconnect without / with a wrong token | 403 `csrf` |
| Valid token + foreign Origin | 403 `cross_origin` |
| Cross-site `<video>` read of a clip, CORS preflight from evil.example | 403, no `Access-Control-*` |
| DNS rebinding (`Host: evil.example`) on `/api/settings` and `/` | 421 `bad_host`, token not served |
| Traversal (`..\..\config.json` in clip / export / music / project / static routes) | 404 / 400 / SPA shell; no file content |
| Upload `..\..\..\evil.exe`, filename with shell characters | 400 |
| Import `http://127.0.0.1:8799/…`, `169.254.169.254`, `192.168.1.1`, `[::ffff:127.0.0.1]`, `localtest.me`, `file:///…` | 400 (refused) |
| XSS strings in a title / OAuth error | escaped; CSP without `unsafe-inline` scripts; JSON sandboxed |
| 70 MB body (declared / chunked) | 413 / 400 |
| The UI's own write with the token (control) | 200 |

The layout, preview and accessibility checks pass with the new CSP and token (288 + 26 views, no
CSP violation in the console).

## What the owner must do

1. **Rotate the Google OAuth client secret** (S-01): Google Cloud Console → Google Auth Platform
   → Clients → the SharkyClips Desktop client → add a new secret, download the JSON, save it as
   `%USERPROFILE%\.sharkyclips\google_client.json`, Settings → YouTube → *Reload Google client*,
   then **delete the old secret** in the console. Same client ID, so the YouTube sign-in stays.
   Then close GitHub secret-scanning alerts #1 / #2 as "revoked".
2. Optional: rewrite history to drop the old file (`git filter-repo`), only after rotating; it
   doesn't help without rotation and breaks every clone.
3. Optional: commit with your GitHub `noreply` address (`git config user.email`); old commits keep
   the current one.
4. Optional: sign release tags (`git tag -s`, SSH or GPG) and set `update_require_signed: true`.

## Residual risks

- LAN mode is plain HTTP; the PIN protects access, not the traffic. Use trusted networks only.
- pip / npm installs aren't hash-pinned (PyPI / npm integrity + https only).
- A program already running as the user can read `~/.sharkyclips` (by design out of scope).
