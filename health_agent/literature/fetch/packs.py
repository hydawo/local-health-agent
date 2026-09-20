"""Download a pack from the project's GitHub Release and verify it.

The pack name and the tool's version are what the request reveals, plus the
caller's IP address to GitHub. Nothing from the data folder is involved.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import packs as pack_format
from . import client

REPO = "hydawo/local-health-agent"
PACK_RELEASE_TAG = "packs-v1"


def release_url(slug: str, version: str) -> str:
    return (f"https://github.com/{REPO}/releases/download/{PACK_RELEASE_TAG}/"
            f"{pack_format.asset_name(slug, version)}")


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def download_url(url: str, dest_dir: Path, *, get=client.get) -> Path:
    """Fetch `url` and `url + ".sha256"`, verify, and return the saved path.

    The digest is checked before the file gets its final name so a bad
    download never looks like a good one on disk: the bytes land in a
    `.part` file that is removed on any failure, not only a mismatch. The
    URL goes through `client.get`, so a host off the allow list is refused
    before anything is written or opened. The saved name is the URL's last
    path segment, so a segment that is empty or a dot is refused up front:
    it would name a directory, not a file. A query string is refused because
    `url + ".sha256"` would then be nonsense.
    """
    dest_dir = Path(dest_dir)
    if "?" in url or "#" in url:
        raise pack_format.PackError(
            f"{url}: a pack URL cannot carry a query string or fragment")
    name = client.url_filename(url)
    if name in ("", ".", ".."):
        raise pack_format.PackError(f"{url}: no file name in the URL path")
    # Read the sidecar first: it is tiny, and it is where a refused host or a
    # missing asset fails, before dest_dir exists or a .part is written.
    sidecar = get(url + ".sha256").decode("utf-8", errors="replace").split()
    expected = sidecar[0].strip().lower() if sidecar else ""
    if not _HEX_DIGEST.match(expected):
        raise pack_format.PackError(
            f"{name}: malformed sidecar; expected a 64-hex-character sha256 "
            f"as the first token of {name}.sha256")
    dest_dir.mkdir(parents=True, exist_ok=True)
    partial = dest_dir / (name + ".part")
    final = dest_dir / name
    try:
        partial.write_bytes(get(url))
        actual = pack_format.sha256_file(partial)
        if actual != expected:
            raise pack_format.PackError(
                f"{name}: downloaded file digest {actual[:12]}... does not "
                f"match the published {expected[:12]}...; nothing was installed")
        partial.replace(final)
    finally:
        # After a successful replace the .part no longer exists; on any
        # exception it does, and must not survive to look like a download
        # someone could resume or trust.
        partial.unlink(missing_ok=True)
    return final


def download(slug: str, version: str, dest_dir: Path, *, get=client.get) -> Path:
    """Fetch a catalog pack's asset and its .sha256 from the project release;
    refuse to keep a file that does not match. See `download_url`."""
    return download_url(release_url(slug, version), dest_dir, get=get)
