"""1.2.52: links pasted into "Import" may only point at the public internet (no SSRF).

A link is handed to yt-dlp, which downloads whatever it points at. Without this check a link
(typed, or planted by someone else) could make SharkyClips fetch from this PC (127.0.0.1:...),
the router, a NAS or a cloud metadata address (169.254.169.254) and turn the answer into a
"video". So before an import:

  * only http:// and https:// (yt-dlp refuses file:// by itself; we refuse it too),
  * no user:password@ in the link,
  * every address the host name resolves to must be a public one (not loopback, private,
    link-local, carrier-grade NAT, multicast, reserved or unspecified), and
  * after yt-dlp's lookup the final media / page address is checked again (a redirect).

Settings key "import_allow_private": true allows LAN addresses (e.g. a NAS with your VODs).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from . import config
from .errors import SharkyClipsError

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
            or not ip.is_global)


def _resolve(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        return []


def check_public_url(url: str, resolve=None) -> str:
    """The link if it's a public http(s) address; raises SharkyClipsError otherwise."""
    text = (url or "").strip()
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise SharkyClipsError("Only http:// and https:// links can be imported.",
                               "Paste the address of the video page, e.g. https://www.twitch.tv/videos/...")
    if parts.username or parts.password:
        raise SharkyClipsError("Links with a user name or password in them can't be imported.")
    if config.get("import_allow_private") is True:
        return text
    host = parts.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise _private()
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        addrs = (resolve or _resolve)(host)
    if not addrs:
        raise SharkyClipsError(f"Couldn't find {host} on the internet.", "Check the link and your connection.")
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a.split("%")[0])
        except ValueError:
            raise _private() from None
        if _blocked(ip):
            raise _private()
    return text


def _private() -> SharkyClipsError:
    return SharkyClipsError("This link points at this computer or your local network, so it can't be imported.",
                            "Import a public video link, or choose the file itself.")
