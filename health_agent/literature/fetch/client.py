"""One HTTP GET, three allowed hosts.

Every byte the corpus ever fetches goes through `get`. Keeping the transport
in one function is what lets THREAT_MODEL.md name the network surface as
exactly three modules and lets a test enforce it; the allow-list is what
makes "a pack request reveals a category, not a person" checkable, since a
URL to anywhere else is refused before a socket opens.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse

from health_agent import __version__

USER_AGENT = f"health-agent/{__version__} (+https://github.com/hydawo/local-health-agent)"

# NCBI for pack builds; GitHub for pack downloads. Release assets redirect to
# objects.githubusercontent.com, which is the only redirect followed.
ALLOWED_HOSTS: frozenset[str] = frozenset({
    "eutils.ncbi.nlm.nih.gov", "github.com", "objects.githubusercontent.com",
})


def query_url(base: str, params: dict) -> str:
    """`base?k=v&...`, with values percent-encoded.

    Lives here, not in the callers, because the guard test forbids any
    `urllib` import outside this file, and `urllib.parse` reads as one even
    though it opens nothing. One file owning all URL handling is a simpler
    rule to check than "transports, but not parsers".
    """
    return base + "?" + urlencode(params)


def url_filename(url: str) -> str:
    """The last path segment of a URL, without query or fragment. Same
    reason as `query_url` for living here."""
    return urlparse(url).path.rsplit("/", 1)[-1]


class FetchError(RuntimeError):
    """Anything that stops a fetch: a refused host, an HTTP status, a
    transport failure. One type so callers can catch the network as a whole
    without knowing which layer said no."""

    def __init__(self, host: str, status: int | None, message: str) -> None:
        super().__init__(f"{host}: {message}" + (f" (HTTP {status})" if status else ""))
        self.host, self.status = host, status


def _allowed(url: str) -> str | None:
    """The host if the URL may be fetched, else None.

    `urlparse(...).hostname` is used rather than `netloc` because it strips
    userinfo and port: `https://github.com@evil.com/` has hostname evil.com,
    and a netloc check would have let it through. Membership is exact, so
    `evil.github.com` and `github.com.evil.com` are refused too. The scheme
    is checked separately so a plain-http URL to an allowed host is refused
    as well: the allow list is a promise about who sees the request, and
    that promise needs TLS.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        return None
    return host


class _AllowListRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a second request, so it gets the same check as the first.
    Without this, an allowed host could bounce the client anywhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _allowed(newurl) is None:
            raise FetchError(urlparse(newurl).hostname or "?", code,
                             "redirect to a host outside the allow list")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float) -> bytes:
    """The one place a socket is opened. Tests replace this function."""
    opener = urllib.request.build_opener(_AllowListRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(request, timeout=timeout) as response:
        return response.read()


def get(url: str, *, timeout: float = 60.0) -> bytes:
    """GET a URL on the allow list over https, or raise FetchError before
    any socket is opened."""
    host = _allowed(url)
    if host is None:
        raise FetchError(urlparse(url).hostname or "?", None,
                         "host is not on the allow list")
    try:
        return _open(url, timeout)
    except urllib.error.HTTPError as exc:
        raise FetchError(host, exc.code, str(exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise FetchError(host, None, str(exc.reason)) from exc
