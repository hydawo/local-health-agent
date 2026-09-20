"""First-run consent for the opt-in cloud tier.

Plan §4 and §5 both require that using the cloud tier is a deliberate act: an
explicit flag, plus a one-time confirmation the first time it is used that spells
out what leaves the machine. A silent flag is not enough, because the whole point
of the two-tier design is that the user knows which tier they are in.

Two decisions worth stating:

**Consent is recorded per data folder, not globally.** It lives next to the index
it applies to. Someone who keeps separate folders for different people has to
consent for each — which is the right default when the thing being sent is
somebody's health data.

**The record stores what was agreed to.** It keeps the notice's version and the
model, so a later change to what gets sent (a new tool, a different provider)
can invalidate the old consent and ask again rather than silently inheriting a
"yes" the user gave to a different question.

**One mechanism, two notices.** The literature packs feature (spec §6) needs
the same one-time confirmation for a different reason: not health data leaving,
but the tool connecting to the internet at all. Rather than a second module, a
`Notice` carries the text, version, and filename, and every function takes one,
defaulting to the cloud notice so the original callers are unchanged. Each
notice has its own file, so agreeing to one never implies the other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Bump when the notice's substance changes — what is sent, where, or to whom.
# A bump invalidates stored consent and re-prompts.
NOTICE_VERSION = 1

CONSENT_FILENAME = "cloud_consent.json"

NOTICE = """\
────────────────────────────────────────────────────────────────────────
  CLOUD MODE — this sends your health data off this machine
────────────────────────────────────────────────────────────────────────

Everything so far has run locally. Cloud mode does not.

WHAT GETS SENT to Anthropic's API, for every question you ask this way:

  • your question, as you typed it
  • an inventory of what your index contains — the metrics you track, the
    lab analytes in your reports, the tags on your notes, and the date
    ranges they cover
  • the results of every tool call the model makes while answering:
    HealthKit values, lab results with their reference ranges, and
    verbatim excerpts from your medical records and personal notes

That last one is the part people underestimate. A question about one lab
value can pull in text from a medical record. You will not see what was
sent until after it is sent.

WHAT DOES NOT GET SENT:

  • your files — the source PDFs, notes, and export stay on this disk
  • anything at all when you are not using --cloud

WHERE IT GOES:

  Anthropic's API. Their terms govern what happens to it there:
  https://www.anthropic.com/legal/commercial-terms
  https://privacy.anthropic.com/

  Read those before agreeing. This tool cannot make promises on their
  behalf, and does not try to.

This is a HYBRID setup, not a local one. If the reason you chose this
tool was that nothing leaves your machine, say no — the local tier
answers the same questions.
────────────────────────────────────────────────────────────────────────"""


@dataclass(frozen=True)
class Notice:
    """One disclosure: what a record of consent is a record of."""

    name: str
    version: int
    filename: str
    text: str
    summary: str


CLOUD = Notice(
    name="cloud",
    version=NOTICE_VERSION,
    filename=CONSENT_FILENAME,
    text=NOTICE,
    summary=("Cloud mode sends query text, a data inventory, and tool "
             "results including record and note excerpts to Anthropic's "
             "API. Source files are not sent."),
)

LITERATURE = Notice(
    name="literature",
    version=1,
    filename="literature_consent.json",
    text="""\
────────────────────────────────────────────────────────────────────────
  LITERATURE PACKS: this command connects to the internet
────────────────────────────────────────────────────────────────────────

Asking questions never touches the network, and `offline-check` proves
it. Two literature commands do, and only when you run them:

  health-agent literature install <pack>
    Downloads one pack file from github.com (the project's releases).
    What the request reveals: which pack you chose, your IP address, and
    this tool's version. Nothing from your data folder. Nothing about
    your questions. Packs are deliberately broad (cardiovascular, sleep)
    so that the choice says as little about you as possible.

  health-agent literature build-pack <pack>
    A maintainer command. Sends the pack's search terms (a fixed list of
    medical subject headings, the same for everyone) to
    eutils.ncbi.nlm.nih.gov, with your IP address and the tool's version.

Neither command runs on its own, checks for updates, or reports usage.
Never during ask, ingest, or search.

Revoke with `health-agent literature-consent --revoke`.
────────────────────────────────────────────────────────────────────────""",
    summary=("Literature install downloads one named pack file from github.com; "
             "build-pack sends the catalog's fixed search terms to "
             "eutils.ncbi.nlm.nih.gov. Each reveals the choice, an IP address, "
             "and the tool's version; nothing from the data folder."),
)


def _current_version(notice: Notice) -> int:
    """The cloud notice's version is read through the module alias, not the
    instance: `NOTICE_VERSION` predates `Notice` and is what a maintainer
    (and the existing test) bumps when the cloud disclosure changes. Reading
    the instance would make that bump silently stop re-prompting."""
    if notice.name == CLOUD.name:
        return NOTICE_VERSION
    return notice.version


@dataclass
class ConsentRecord:
    granted_at: str
    notice_version: int
    model: str
    notice_name: str = CLOUD.name

    def is_current(self, notice: Notice = CLOUD) -> bool:
        # The name check is what makes a record answer only the question it
        # was written for: a file copied between the two paths, or one
        # written before names were stored and read for the other notice,
        # is not consent to this notice.
        return (self.notice_name == notice.name
                and self.notice_version == _current_version(notice))


def consent_path(index_dir: Path, notice: Notice = CLOUD) -> Path:
    return Path(index_dir) / notice.filename


def load(index_dir: Path, notice: Notice = CLOUD) -> ConsentRecord | None:
    path = consent_path(index_dir, notice)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return ConsentRecord(
            granted_at=data["granted_at"],
            notice_version=int(data["notice_version"]),
            model=data.get("model", ""),
            # Records written before the second notice existed have no name
            # and could only have been the cloud one.
            notice_name=data.get("notice_name", CLOUD.name),
        )
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        # An unreadable record is treated as no record. Failing closed here
        # means the worst case is asking again, not sending without asking.
        return None


def record(index_dir: Path, model: str, notice: Notice = CLOUD) -> ConsentRecord:
    path = consent_path(index_dir, notice)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = ConsentRecord(
        granted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        notice_version=_current_version(notice),
        model=model,
        notice_name=notice.name,
    )
    path.write_text(json.dumps({
        "granted_at": entry.granted_at,
        "notice_version": entry.notice_version,
        "notice_name": entry.notice_name,
        "model": entry.model,
        "notice": notice.summary,
    }, indent=2))
    return entry


def revoke(index_dir: Path, notice: Notice = CLOUD) -> bool:
    path = consent_path(index_dir, notice)
    if path.exists():
        path.unlink()
        return True
    return False


def needs_prompt(index_dir: Path, notice: Notice = CLOUD) -> bool:
    existing = load(index_dir, notice)
    return existing is None or not existing.is_current(notice)
