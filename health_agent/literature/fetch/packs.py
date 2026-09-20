"""Download a pack from the project's GitHub Release and verify it.

The pack name and the tool's version are what the request reveals, plus the
caller's IP address to GitHub. Nothing from the data folder is involved.
"""

from __future__ import annotations

from pathlib import Path

from .. import packs as pack_format
from . import client

REPO = "hydawo/local-health-agent"
PACK_RELEASE_TAG = "packs-v1"


def release_url(slug: str, version: str) -> str:
    return (f"https://github.com/{REPO}/releases/download/{PACK_RELEASE_TAG}/"
            f"{pack_format.asset_name(slug, version)}")


def download_url(url: str, dest_dir: Path, *, get=client.get) -> Path:
    """Fetch `url` and `url + ".sha256"`, verify, and return the saved path.

    The digest is checked before the file gets its final name so a bad
    download never looks like a good one on disk: the bytes land in a
    `.part` file that is removed on mismatch. The URL goes through
    `client.get`, so a host off the allow list is refused before anything
    is written or opened.
    """
    dest_dir = Path(dest_dir)
    name = client.url_filename(url)
    if not name:
        raise pack_format.PackError(f"{url}: no file name in the URL path")
    # Read the sidecar first: it is tiny, and it is where a refused host or a
    # missing asset fails, before dest_dir exists or a .part is written.
    sidecar = get(url + ".sha256").decode("utf-8", errors="replace").split()
    if not sidecar:
        raise pack_format.PackError(f"{name}: the published .sha256 is empty")
    expected = sidecar[0]
    dest_dir.mkdir(parents=True, exist_ok=True)
    partial = dest_dir / (name + ".part")
    partial.write_bytes(get(url))
    actual = pack_format.sha256_file(partial)
    if actual != expected:
        partial.unlink()
        raise pack_format.PackError(
            f"{name}: downloaded file digest {actual[:12]}... does not "
            f"match the published {expected[:12]}...; nothing was installed")
    final = dest_dir / name
    partial.replace(final)
    return final


def download(slug: str, version: str, dest_dir: Path, *, get=client.get) -> Path:
    """Fetch a catalog pack's asset and its .sha256 from the project release;
    refuse to keep a file that does not match. See `download_url`."""
    return download_url(release_url(slug, version), dest_dir, get=get)
