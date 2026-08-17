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


@dataclass
class ConsentRecord:
    granted_at: str
    notice_version: int
    model: str

    def is_current(self) -> bool:
        return self.notice_version == NOTICE_VERSION


def consent_path(index_dir: Path) -> Path:
    return Path(index_dir) / CONSENT_FILENAME


def load(index_dir: Path) -> ConsentRecord | None:
    path = consent_path(index_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return ConsentRecord(
            granted_at=data["granted_at"],
            notice_version=int(data["notice_version"]),
            model=data.get("model", ""),
        )
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        # An unreadable record is treated as no record. Failing closed here
        # means the worst case is asking again, not sending without asking.
        return None


def record(index_dir: Path, model: str) -> ConsentRecord:
    path = consent_path(index_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = ConsentRecord(
        granted_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        notice_version=NOTICE_VERSION,
        model=model,
    )
    path.write_text(json.dumps({
        "granted_at": entry.granted_at,
        "notice_version": entry.notice_version,
        "model": entry.model,
        "notice": ("Cloud mode sends query text, a data inventory, and tool "
                   "results including record and note excerpts to Anthropic's "
                   "API. Source files are not sent."),
    }, indent=2))
    return entry


def revoke(index_dir: Path) -> bool:
    path = consent_path(index_dir)
    if path.exists():
        path.unlink()
        return True
    return False


def needs_prompt(index_dir: Path) -> bool:
    existing = load(index_dir)
    return existing is None or not existing.is_current()
