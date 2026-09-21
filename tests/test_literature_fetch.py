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


def test_efetch_reports_a_non_xml_page_as_a_typed_error():
    """A 200 whose body is not MEDLINE XML (an HTML error page, a cut-off
    response) is a fetch failure to `build-pack`, not a parser traceback."""
    def get(url, **kw):
        return b"<html><body>Service unavailable<br></body></html>"
    handle = eutils.SearchHandle(count=10, webenv="W", query_key="1")
    with pytest.raises(client.FetchError) as excinfo:
        eutils.fetch_all(handle, max_articles=10, get=get, sleep=lambda s: None)
    assert excinfo.value.host == "eutils.ncbi.nlm.nih.gov"
    assert "not MEDLINE XML" in str(excinfo.value)
    assert "could not parse" in str(excinfo.value)


def test_esearch_sorts_by_date_only_for_a_capped_pack():
    """`sample` takes the first 2,000 of its matches, so its order has to
    be one the catalog can describe; an uncapped pack takes every match."""
    seen = []

    def get(url, **kw):
        seen.append(url)
        return (FIX / "esearch.xml").read_bytes()

    sample, sleep = pack_format.CATALOG["sample"], pack_format.CATALOG["sleep"]
    assert sample.max_articles and not sleep.max_articles
    eutils.search(sample.search_term(), sort=sample.sort, get=get)
    eutils.search(sleep.search_term(), sort=sleep.sort, get=get)
    assert "sort=pub_date" in seen[0]
    assert "sort=" not in seen[1]


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


@pytest.mark.parametrize("host", [
    "objects.githubusercontent.com",
    # Where GitHub actually sent the first real install; the allow list
    # refused it, which is the behaviour, and this is the fix.
    "release-assets.githubusercontent.com",
])
def test_redirect_to_an_asset_host_keeps_our_user_agent(host):
    import urllib.request
    handler = client._AllowListRedirects()
    req = urllib.request.Request("https://github.com/x",
                                 headers={"User-Agent": client.USER_AGENT})
    new = handler.redirect_request(
        req, _FakeResponse(), 302, "Found", {}, f"https://{host}/asset")
    assert isinstance(new, urllib.request.Request)
    assert new.get_header("User-agent") == client.USER_AGENT
    assert new.full_url == f"https://{host}/asset"


def test_redirect_to_an_unlisted_githubusercontent_host_is_refused():
    """The allow list is exact names, not a suffix: `evil.githubusercontent.com`
    would otherwise pass."""
    import urllib.request
    handler = client._AllowListRedirects()
    req = urllib.request.Request("https://github.com/x")
    with pytest.raises(client.FetchError):
        handler.redirect_request(req, _FakeResponse(), 302, "Found", {},
                                 "https://evil.githubusercontent.com/asset")


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


# --------------------------------------------------------------------------- #
# NCBI's 10,000-record page limit
# --------------------------------------------------------------------------- #

def _esearch_xml(count: int) -> bytes:
    return (f"<eSearchResult><Count>{count}</Count><RetMax>0</RetMax>"
            f"<WebEnv>W{count}</WebEnv><QueryKey>1</QueryKey></eSearchResult>"
            ).encode()


def _counting_get(counts_by_range: dict, whole: int):
    """A fake NCBI: esearch answers from `counts_by_range` keyed by
    (mindate, maxdate), `whole` when no date range is sent; efetch serves
    the fixture batch and records every retstart it was asked for."""
    from urllib.parse import parse_qs, urlparse
    seen = {"retstarts": [], "ranges": []}

    def get(url, **kw):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if "esearch" in url:
            if "mindate" in q:
                key = (q["mindate"], q["maxdate"])
                seen["ranges"].append(key)
                assert q["datetype"] == "pdat"
                return _esearch_xml(counts_by_range.get(key, 0))
            return _esearch_xml(whole)
        seen["retstarts"].append(int(q["retstart"]))
        assert int(q["retstart"]) < eutils.PAGE_LIMIT, "paged past NCBI's limit"
        return (FIX / "efetch_batch.xml").read_bytes()
    return get, seen


def test_a_result_under_the_limit_is_one_handle():
    get, seen = _counting_get({}, whole=9_000)
    handles = eutils.search_slices("x", first_year=2015, last_year=2016, get=get,
                                   sleep=lambda s: None)
    assert [h.count for h in handles] == [9_000]
    assert seen["ranges"] == []


def test_a_result_over_the_limit_is_sliced_by_year_then_month():
    counts = {("2015/01/01", "2015/12/31"): 4_000,
              ("2016/01/01", "2016/12/31"): 12_000,   # over: split into months
              ("2017/01/01", "2017/12/31"): 0}        # empty year: skipped
    counts.update({(f"2016/{m:02d}/01", hi): 1_000 for m, hi in
                   [(1, "2016/01/31"), (2, "2016/02/29"), (3, "2016/03/31"),
                    (4, "2016/04/30"), (5, "2016/05/31"), (6, "2016/06/30"),
                    (7, "2016/07/31"), (8, "2016/08/31"), (9, "2016/09/30"),
                    (10, "2016/10/31"), (11, "2016/11/30"), (12, "2016/12/31")]})
    get, seen = _counting_get(counts, whole=16_000)
    handles = eutils.search_slices("x", first_year=2015, last_year=2017, get=get,
                                   sleep=lambda s: None)
    assert [h.count for h in handles] == [4_000] + [1_000] * 12
    assert ("2016/02/01", "2016/02/29") in seen["ranges"]   # leap year handled


def test_a_month_over_the_limit_raises_rather_than_truncating():
    counts = {("2015/01/01", "2015/12/31"): 20_000}
    counts.update({(f"2015/{m:02d}/01", f"2015/{m:02d}/{d}"): 11_000
                   for m, d in [(1, "31")]})
    get, _ = _counting_get(counts, whole=20_000)
    with pytest.raises(client.FetchError) as excinfo:
        eutils.search_slices("x", first_year=2015, last_year=2015, get=get,
                             sleep=lambda s: None)
    assert "page limit" in str(excinfo.value)


def test_fetch_term_pages_every_slice_under_the_limit_and_dedupes():
    counts = {("2015/01/01", "2015/12/31"): 9_000,
              ("2016/01/01", "2016/12/31"): 9_000}
    get, seen = _counting_get(counts, whole=18_000)
    progress = []
    total, articles = eutils.fetch_term(
        "x", sort=None, max_articles=None, first_year=2015, get=get,
        sleep=lambda s: None, progress=lambda d, t: progress.append((d, t)))
    assert total == 18_000
    assert max(seen["retstarts"]) < eutils.PAGE_LIMIT
    assert len(seen["retstarts"]) == 36                    # 18 pages of 500 per slice
    # the same fixture on every page collapses to its two PMIDs
    assert [a.pmid for a in articles] == ["40000001", "40000002"]
    assert progress[-1] == (18_000, 18_000)
    assert all(d <= t for d, t in progress)


def test_fetch_term_keeps_a_capped_pack_on_one_sorted_handle():
    urls = []

    def get(url, **kw):
        urls.append(url)
        if "esearch" in url:
            return _esearch_xml(50_000)
        return (FIX / "efetch_batch.xml").read_bytes()

    total, articles = eutils.fetch_term(
        "x", sort="pub_date", max_articles=1_000, first_year=None, get=get,
        sleep=lambda s: None)
    assert total == 50_000
    assert sum("esearch" in u for u in urls) == 1
    assert "sort=pub_date" in urls[0] and "mindate" not in urls[0]


# --------------------------------------------------------------------------- #
# Transient NCBI failures
# --------------------------------------------------------------------------- #

def test_esearch_retries_a_transient_backend_failure():
    """The metabolic pack died on one 'Request to GWSearch failed' answer."""
    answers = [b"<eSearchResult><ERROR>Search Backend failed: GWSearch response "
               b"processing error: Request to GWSearch failed.</ERROR></eSearchResult>",
               (FIX / "esearch.xml").read_bytes()]
    slept = []
    handle = eutils.search("x", get=lambda url, **kw: answers.pop(0), sleep=slept.append)
    assert handle.count == 3
    assert slept == [eutils.RETRY_PAUSE_SECONDS]


def test_efetch_retries_a_5xx_and_gives_up_after_the_budget():
    calls = []

    def flaky(url, **kw):
        calls.append(url)
        raise client.FetchError("eutils.ncbi.nlm.nih.gov", 502, "Bad Gateway")

    handle = eutils.SearchHandle(count=10, webenv="W", query_key="1")
    with pytest.raises(client.FetchError) as excinfo:
        eutils.fetch_all(handle, max_articles=10, get=flaky, sleep=lambda s: None)
    assert excinfo.value.status == 502
    assert len(calls) == eutils.RETRIES + 1


def test_a_refused_host_or_a_4xx_is_not_retried():
    calls = []

    def refused(url, **kw):
        calls.append(url)
        raise client.FetchError("eutils.ncbi.nlm.nih.gov", 400, "Bad Request")

    with pytest.raises(client.FetchError):
        eutils.search("x", get=refused, sleep=lambda s: None)
    assert len(calls) == 1
