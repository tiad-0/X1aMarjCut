# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it privately through GitHub:
**[Report a vulnerability](https://github.com/tiad-0/X1aMarjCut/security/advisories/new)**
(repository → *Security* → *Report a vulnerability*). Only the maintainer sees it.

Helpful to include: the SharkyClips version (Settings → Updates), your OS, the steps or a small
proof of concept, and what an attacker gains. You'll get an answer within a week. Fixes ship as a
normal update (Settings → Updates); you're credited in the advisory unless you'd rather not be.

Only the latest version is supported. Please don't test against other people's computers or
accounts, and don't access or keep data that isn't yours.

## What SharkyClips exposes, and how it's protected

SharkyClips is a local app: a web server on your own computer that your browser talks to.

- **Listens on `127.0.0.1` only** by default. Nothing outside your computer can reach it.
- **Other websites in your browser can't use it** (since 1.2.52): requests whose `Host` isn't
  localhost / an IP address / this computer's name are refused (DNS rebinding); writes from
  another origin are refused (Origin + Fetch Metadata checks); every write needs the per-install
  API token that only the SharkyClips page itself can read; other sites can't embed or read its
  videos, images or JSON; no CORS headers are ever sent. A strict Content-Security-Policy,
  `frame-ancestors 'none'`, `nosniff` and `Referrer-Policy: no-referrer` protect the UI.
- **`--lan` mode** (`python cli.py serve --lan`) lets a phone on your network open the UI. Other
  devices get nothing until you set an **access PIN or password** (Settings → Other devices). It is
  stored only as a salted scrypt hash, logins are rate-limited with a growing lockout, sessions
  are `HttpOnly` + `SameSite=Strict` cookies. Some actions never work from another device
  (local file paths, connecting accounts, the Google client, quitting, shutdown). LAN mode is plain
  HTTP: use it only on networks you trust.
- **Your accounts' tokens** (YouTube, Instagram, TikTok, Telegram bot) live only in your settings
  folder (`~/.sharkyclips`), encrypted with Windows DPAPI / the macOS Keychain. They are never sent
  to the UI, never logged (a redaction filter masks anything token-like in logs and error
  messages) and only ever sent to the platform they belong to.
- **YouTube** uses *your own* Google project (SharkyClips ships no Google client); see
  [docs/youtube-own-project.md](docs/youtube-own-project.md). Sign-in: OAuth with PKCE + `state`,
  redirect to `127.0.0.1` only.
- **Imports** ("paste a link") only fetch public internet addresses: links to this computer, your
  local network or cloud metadata addresses are refused (SSRF).
- **Updates** come only from this repository's `main` branch, and only a tagged release commit is
  installed (`git merge --ff-only` to exactly that commit).
- **Automatic TikTok posting** stops at any CAPTCHA, verification or login screen and hands over
  to you; it never tries to solve or bypass them.

The full audit (findings, severities, fixes): [docs/SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md).

## Out of scope

Anything that needs an attacker to already run code on your computer, control your user account
or your settings folder, or to be on a LAN where you run `--lan` with a weak PIN you shared.
