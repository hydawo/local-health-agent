"""The corpus's only network code. Nothing here opens a socket: `get` is the seam."""
from __future__ import annotations

from pathlib import Path

import pytest

from health_agent.literature import packs as pack_format
from health_agent.literature.fetch import client, eutils
from health_agent.literature.fetch import packs as fetch_packs

FIX = Path(__file__).parent / "fixtures" / "literature"
# Well-formed (64 hex) but matching nothing: exercises the mismatch path, not
# the malformed-sidecar refusal that fires first.
WRONG_DIGEST = ("0" * 64 + "  x\n").encode()


class _FakeResponse:
    def __init__(self):
        self.read_called = self.closed = False

    def read(self):
        self.read_called = True
        return b""

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# client: the allow list is checked before any socket opens
# --------------------------------------------------------------------------- #

def test_client_refuses_hosts_outside_the_allow_list(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_open", lambda url, timeout: calls.append(url) or b"")
    with pytest.raises(client.FetchError):
        client.get("https://example.com/anything")
    assert calls == []


@pytest.mark.parametrize("url", [
    # userinfo trick: urlparse's `hostname` is evil.com, not github.com
    "https://github.com@evil.com/hydawo/x",
    # suffix trick: exact match, not "ends with an allowed host"
    "https://evil.github.com/x",
    "https://github.com.evil.com/x",
    # scheme: plain http to an allowed host is still refused
    "http://github.com/hydawo/x",
    "https://github.com:443@evil.com/x",
])
def test_client_refuses_lookalike_urls_before_opening(monkeypatch, url):
    calls = []
    monkeypatch.setattr(client, "_open", lambda u, timeout: calls.append(u) or b"")
    with pytest.raises(client.FetchError):
        client.get(url)
    assert calls == []


def test_client_passes_an_allowed_https_url_through(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_open", lambda u, timeout: calls.append(u) or b"ok")
    assert client.get("https://github.com/hydawo/x") == b"ok"
    assert calls == ["https://github.com/hydawo/x"]


def test_client_refuses_a_redirect_off_the_allow_list():
    handler = client._AllowListRedirects()
    with pytest.raises(client.FetchError) as excinfo:
        handler.redirect_request(None, _FakeResponse(), 302, "Found", {},
                                 "https://evil.com/asset")
    assert excinfo.value.host == "evil.com"
    assert excinfo.value.status == 302


def test_client_user_agent_names_the_tool():
    from health_agent import __version__
    assert "health-agent" in client.USER_AGENT and __version__ in client.USER_AGENT


# --------------------------------------------------------------------------- #
# eutils
# --------------------------------------------------------------------------- #

def test_esearch_parses_count_and_history_handle():
    handle = eutils.search("x", get=lambda url, **kw: (FIX / "esearch.xml").read_bytes())
    assert handle.count == 3
    assert handle.webenv == "MCID_synthetic_webenv"
    assert handle.query_key == "1"


def test_efetch_pages_in_batches_with_a_polite_delay():
    seen_urls, slept = [], []

    def get(url, **kw):
        seen_urls.append(url)
        return (FIX / "efetch_batch.xml").read_bytes()

    handle = eutils.SearchHandle(count=1200, webenv="W", query_key="1")
    articles = eutils.fetch_all(handle, max_articles=1000, get=get,
                                sleep=slept.append, batch=500)
    assert len(seen_urls) == 2                     # 1000 capped, 500 per batch
    assert "retstart=500" in seen_urls[1]
    assert all("WebEnv=W" in u and "query_key=1" in u for u in seen_urls)
    assert len(slept) == 1                         # between batches, not after the last
    # Unique PMIDs in first-seen order: both batches serve the same fixture,
    # so the four parsed records collapse to two.
    assert [a.pmid for a in articles] == ["40000001", "40000002"]


def test_efetch_reports_a_non_200_as_a_typed_error():
    def get(url, **kw):
        raise client.FetchError("eutils.ncbi.nlm.nih.gov", 503, "busy")
    handle = eutils.SearchHandle(count=10, webenv="W", query_key="1")
    with pytest.raises(client.FetchError) as excinfo:
        eutils.fetch_all(handle, max_articles=10, get=get, sleep=lambda s: None)
    assert excinfo.value.host == "eutils.ncbi.nlm.nih.gov"


# --------------------------------------------------------------------------- #
# fetch.packs
# --------------------------------------------------------------------------- #

def test_release_url_points_at_the_repo_release_asset():
    url = fetch_packs.release_url("sleep", "2026.09")
    assert url.startswith("https://github.com/")
    assert url.endswith(f"/releases/download/{fetch_packs.PACK_RELEASE_TAG}/sleep-2026.09.jsonl.gz")


def test_download_verifies_the_sidecar_digest_before_returning(tmp_path):
    pack_path = tmp_path / "src" / "sleep-1.jsonl.gz"
    pack_format.write_pack(pack_path, pack_format.CATALOG["sleep"], "1", [],
                           license="L")
    good = pack_format.sha256_file(pack_path)
    served = {fetch_packs.release_url("sleep", "1"): pack_path.read_bytes(),
              fetch_packs.release_url("sleep", "1") + ".sha256":
                  f"{good}  sleep-1.jsonl.gz\n".encode()}
    out = fetch_packs.download("sleep", "1", tmp_path / "dl", get=lambda u, **kw: served[u])
    assert out.exists() and pack_format.sha256_file(out) == good

    served[fetch_packs.release_url("sleep", "1") + ".sha256"] = WRONG_DIGEST
    with pytest.raises(pack_format.PackError):
        fetch_packs.download("sleep", "1", tmp_path / "dl2", get=lambda u, **kw: served[u])
    assert not (tmp_path / "dl2" / "sleep-1.jsonl.gz").exists()
    assert list((tmp_path / "dl2").iterdir()) == []   # no .part left behind either


def test_download_url_fetches_an_arbitrary_asset_and_verifies_it(tmp_path):
    pack_path = tmp_path / "src" / "sleep-1.jsonl.gz"
    pack_format.write_pack(pack_path, pack_format.CATALOG["sleep"], "1", [],
                           license="L")
    good = pack_format.sha256_file(pack_path)
    url = "https://github.com/someone/fork/releases/download/x/sleep-1.jsonl.gz"
    served = {url: pack_path.read_bytes(),
              url + ".sha256": f"{good}  sleep-1.jsonl.gz\n".encode()}
    out = fetch_packs.download_url(url, tmp_path / "dl", get=lambda u, **kw: served[u])
    assert out == tmp_path / "dl" / "sleep-1.jsonl.gz"
    assert pack_format.sha256_file(out) == good

    served[url + ".sha256"] = WRONG_DIGEST
    with pytest.raises(pack_format.PackError):
        fetch_packs.download_url(url, tmp_path / "dl2", get=lambda u, **kw: served[u])
    assert list((tmp_path / "dl2").iterdir()) == []


def test_download_url_treats_an_empty_sidecar_as_a_pack_error(tmp_path):
    url = "https://github.com/someone/fork/releases/download/x/sleep-1.jsonl.gz"
    served = {url: b"irrelevant", url + ".sha256": b"\n"}
    with pytest.raises(pack_format.PackError):
        fetch_packs.download_url(url, tmp_path / "dl", get=lambda u, **kw: served[u])
    assert not (tmp_path / "dl").exists()


def test_download_url_refuses_a_host_off_the_allow_list(tmp_path, monkeypatch):
    """`download_url` goes through `client.get` by default, so the allow list
    applies to a user-supplied URL exactly as it does to the catalog's."""
    calls = []
    monkeypatch.setattr(client, "_open", lambda u, timeout: calls.append(u) or b"")
    with pytest.raises(client.FetchError):
        fetch_packs.download_url("https://evil.com/sleep-1.jsonl.gz", tmp_path / "dl")
    assert calls == []
    assert not (tmp_path / "dl").exists() or list((tmp_path / "dl").iterdir()) == []


# --------------------------------------------------------------------------- #
# Hardening before a user-supplied URL can reach the client (Task 4's --from)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "https://user@github.com/x",            # userinfo on an allowed host
    "https://user:pw@github.com/x",
    "https://[github.com]/x",               # malformed: ValueError inside urlparse
])
def test_client_refuses_userinfo_and_malformed_urls_as_typed_errors(monkeypatch, url):
    calls = []
    monkeypatch.setattr(client, "_open", lambda u, timeout: calls.append(u) or b"")
    with pytest.raises(client.FetchError):
        client.get(url)
    assert calls == []


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    ConnectionResetError(54, "reset"),
])
def test_client_wraps_read_phase_os_errors(monkeypatch, exc):
    def boom(u, timeout):
        raise exc
    monkeypatch.setattr(client, "_open", boom)
    with pytest.raises(client.FetchError) as excinfo:
        client.get("https://github.com/x")
    assert excinfo.value.host == "github.com"
    assert excinfo.value.status is None


def test_client_wraps_an_incomplete_read(monkeypatch):
    import http.client

    def boom(u, timeout):
        raise http.client.IncompleteRead(b"partial")
    monkeypatch.setattr(client, "_open", boom)
    with pytest.raises(client.FetchError):
        client.get("https://github.com/x")


@pytest.mark.parametrize("newurl", [
    "https://evil.com/asset",
    "http://github.com/hydawo/x",           # allowed host, wrong scheme
])
def test_redirect_refusal_drains_and_closes_the_response(newurl):
    import urllib.request
    handler = client._AllowListRedirects()
    req = urllib.request.Request("https://github.com/x",
                                 headers={"User-Agent": client.USER_AGENT})
    fp = _FakeResponse()
    with pytest.raises(client.FetchError):
        handler.redirect_request(req, fp, 302, "Found", {}, newurl)
    assert fp.read_called and fp.closed


def test_redirect_to_the_asset_host_keeps_our_user_agent():
    import urllib.request
    handler = client._AllowListRedirects()
    req = urllib.request.Request("https://github.com/x",
                                 headers={"User-Agent": client.USER_AGENT})
    new = handler.redirect_request(
        req, _FakeResponse(), 302, "Found", {},
        "https://objects.githubusercontent.com/asset")
    assert isinstance(new, urllib.request.Request)
    assert new.get_header("User-agent") == client.USER_AGENT
    assert new.full_url == "https://objects.githubusercontent.com/asset"


@pytest.mark.parametrize("url", [
    "https://github.com/a/b/",                       # empty final segment
    "https://github.com/a/.",
    "https://github.com/a/..",
    "https://github.com/a/sleep-1.jsonl.gz?x=1",     # query string
])
def test_download_url_refuses_unusable_names_before_any_fetch(tmp_path, url):
    calls = []
    with pytest.raises(pack_format.PackError):
        fetch_packs.download_url(url, tmp_path / "dl", get=lambda u, **kw: calls.append(u) or b"")
    assert calls == []
    assert not (tmp_path / "dl").exists()


def test_download_url_removes_the_partial_file_on_any_failure(tmp_path, monkeypatch):
    url = "https://github.com/a/sleep-1.jsonl.gz"
    served = {url: b"bytes", url + ".sha256": ("0" * 64 + "  sleep-1.jsonl.gz\n").encode()}

    def boom(path):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(pack_format, "sha256_file", boom)
    with pytest.raises(RuntimeError):
        fetch_packs.download_url(url, tmp_path / "dl", get=lambda u, **kw: served[u])
    assert list((tmp_path / "dl").iterdir()) == []


@pytest.mark.parametrize("sidecar", [
    b"deadbeef  x\n",                  # too short
    b"z" * 64 + b"\n",                 # not hex
    b"\n",                             # empty
])
def test_download_url_rejects_a_malformed_sidecar(tmp_path, sidecar):
    url = "https://github.com/a/sleep-1.jsonl.gz"
    served = {url: b"bytes", url + ".sha256": sidecar}
    with pytest.raises(pack_format.PackError, match="malformed sidecar"):
        fetch_packs.download_url(url, tmp_path / "dl", get=lambda u, **kw: served[u])
    assert not (tmp_path / "dl").exists()


def test_download_url_accepts_an_upper_case_padded_sidecar(tmp_path):
    pack_path = tmp_path / "src" / "sleep-1.jsonl.gz"
    pack_format.write_pack(pack_path, pack_format.CATALOG["sleep"], "1", [], license="L")
    good = pack_format.sha256_file(pack_path)
    url = "https://github.com/a/sleep-1.jsonl.gz"
    served = {url: pack_path.read_bytes(),
              url + ".sha256": f"  {good.upper()}  sleep-1.jsonl.gz\r\n".encode()}
    out = fetch_packs.download_url(url, tmp_path / "dl", get=lambda u, **kw: served[u])
    assert pack_format.sha256_file(out) == good


def test_esearch_surfaces_ncbis_own_error_text():
    body = (b'<?xml version="1.0"?><eSearchResult><Count>0</Count>'
            b'<ERROR>Invalid db name specified: pubmedx</ERROR></eSearchResult>')
    with pytest.raises(client.FetchError, match="Invalid db name specified: pubmedx"):
        eutils.search("x", get=lambda url, **kw: body)


def test_looks_like_url_routes_schemes_not_prefixes():
    from health_agent.literature.fetch import client
    assert client.looks_like_url("https://github.com/x.jsonl.gz")
    assert client.looks_like_url("http://github.com/x.jsonl.gz")
    assert not client.looks_like_url("/tmp/sleep-1.jsonl.gz")
    assert not client.looks_like_url("sleep-1.jsonl.gz")
    assert not client.looks_like_url("~/packs/sleep-1.jsonl.gz")
