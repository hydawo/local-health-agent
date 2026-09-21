# Literature refresh: the corpus stays current without a new release

Until now the only way a person's corpus got newer was a newer pack
version on the release, which means the maintainer rebuilding and
uploading five files. The maintainer has said plainly that they do not
know how often that will happen. So `health-agent literature refresh`
goes to NCBI from the person's own machine, fetches what PubMed has added
to each installed pack's query since the last refresh, and folds it in.
The corpus is as current as the last time the person ran the command,
whatever the release says.

Decided with Hassan on 2026-09-20 (option B over "refresh from the
release"), with the privacy cost stated: this is a second party that
learns which packs a person has installed, and a repeated signal rather
than a one-time download. Section 5 is that cost, written down.

## 1. Command

```
health-agent literature refresh [slug ...] [--since YYYY-MM-DD] [--yes]
                                [--no-embed] [--embed-backend NAME]
```

- No slugs: every installed pack that the catalog knows and that is not
  `sample`. Named slugs: those, in that order; a slug that is not
  installed is an error (exit 2) before any request. `sample` named
  explicitly prints "sample is a fixed snapshot of 2,000 articles;
  install a topical pack to have something to refresh" and is skipped,
  exit 0.
- Asks under the literature consent notice, version 2 (§5). Once.
- `--since` overrides the window start for every pack named (§3). Meant
  for the first refresh of a corpus built before this command existed,
  and for tests.
- Per pack, prints the window, how many PubMed matched, how many were
  added, how many existing articles were marked retracted, and the chunk
  and embedding counts. Exit 0 when every named pack refreshed; 2 on a
  fetch failure, after any packs already refreshed have been committed.
  A failed pack leaves the corpus as it was for that pack.

## 2. What is fetched

Two queries per pack, both through `literature/fetch/eutils.py` and its
sliced, retrying `fetch_term`:

**Additions.** The pack's `search_term()` (topic, evidence filter,
`2015:3000[dp]`, `hasabstract`) with an Entrez-date window,
`datetype=edat`, from the window start to today. Entrez date is the day
PubMed added the record, which is what "new since my last refresh" means;
publication date would miss a 2025 paper indexed in 2026. Articles whose
PMID is already in the corpus are upserted (title, tier, license, and
retraction state refreshed, as `build()` does) and linked to the pack;
new PMIDs are inserted and linked. Nothing is unlinked: refresh adds, it
never removes.

**Retractions.** The pack's topic query AND `"Retracted Publication"[Publication Type]`,
no date window, no evidence filter (a retracted trial is retracted
whatever its tier). Every returned PMID that exists in the corpus gets
`retracted = 1` and the parser's `retraction_note`. Articles the corpus
does not hold are ignored. This is the part that makes refresh worth
running on a corpus that has not changed: a snapshot cites a retracted
paper forever, and `search_medical_literature` already carries the
must-say caveat for a row marked retracted.

## 3. The window

From the pack's last `refreshed_at`, else its `built_at`, as a date, minus
one day (an overlap, so a record indexed on the boundary day is not
missed; the upsert makes the overlap harmless), to today. `--since` replaces
the start. A pack built by `build-pack` records `built_at` at build time,
so the first refresh of a freshly installed pack covers exactly the gap
since the maintainer's fetch.

## 4. Storage: schema v5, migrated in place

- `pack.refreshed_at TEXT` (ISO datetime, null until the first refresh).
- New table `refresh_log(id, pack_id → pack, ran_at, window_from,
  window_to, matched, added, retracted)`. One row per pack per run. This
  is the "what changed on each refresh" that ROADMAP #5 asks for, in its
  smallest useful form.
- `LITERATURE_SCHEMA_VERSION = 5`. `schema.connect()` migrates a v4 file
  in place (`ALTER TABLE pack ADD COLUMN`, `CREATE TABLE refresh_log`,
  stamp 5) instead of refusing it. Every earlier bump refused and asked
  for `--rebuild`; that was fine when a corpus was a 1.6 MB download and
  is not now that `cardiovascular` is 62 MB. A version older than 4 still
  refuses, as before.

`corpus.py` gains `add(conn, articles, *, slug) -> BuildStats`, which is
`build()` without the version and `built_at` update and without the
unlink pass, and `mark_retracted(conn, notes: dict[str, str | None]) ->
int`. The upsert loop is shared between `build()` and `add()`, not copied.
`add()` sets `refreshed_at` and writes the `refresh_log` row; the FTS
rebuild and the commit happen the same way `build()` does them.

After the additions land, chunks are embedded exactly as `install` does
it (`embed_corpus`, then reclaim orphans), unless `--no-embed`.

## 5. What it reveals, and the notice

New network fact, stated everywhere the old ones are:

- To `eutils.ncbi.nlm.nih.gov`: the pack's fixed search terms (the same
  for everyone), a date range, your IP address, the tool's version, and
  `NCBI_API_KEY` if set. Because you only refresh packs you have installed,
  NCBI can infer which body-system areas you chose. That is the same class
  of information GitHub already sees at install, and it is why packs stay
  broad. It is a second party, and it repeats on every refresh.
- Never on its own. Nothing schedules a refresh; the tool does not check
  whether one is due.

`consent.LITERATURE` becomes version 2: the notice gains a `refresh`
paragraph saying the above in the notice's own register, "Two literature
commands" becomes three, and the summary is updated. Existing consent is
version 1, so the next literature command re-asks; that is the mechanism
working as designed. `THREAT_MODEL.md` Claim 6 gains the refresh bullet
and its title changes from "two commands" to "three". `offline-check`'s
description and the `check` command's network-posture text name three
commands.

## 6. Where it shows

- `literature status`: one line per pack, `slug@version  built
  2026-09-20  refreshed 2026-10-04 (+312, 2 retracted)` or `never
  refreshed`.
- `literature packs`: installed packs show days since build or refresh.
- `check`: the packs line gains staleness, e.g. `cardiovascular@2026.09
  (refreshed 14 days ago)`, `sleep@2026.09 (never refreshed)`.
- README literature section: a short paragraph on refresh, what it sends,
  and that `sample` is a snapshot. ROADMAP #1's refresh bullet and #5's
  change-tracking bullet marked done in their smallest form.

## 7. Where the code goes

- `health_agent/literature/schema.py`: v5, `_migrate_4_to_5`, called from
  `connect()` when `read_version() == 4`.
- `health_agent/literature/corpus.py`: `add`, `mark_retracted`, the shared
  upsert; `coverage()` reports `refreshed_at` per pack.
- `health_agent/literature/fetch/eutils.py`: `search()` takes `datetype`
  (default `pdat`); `fetch_term()` passes `datetype`, `mindate`, `maxdate`
  through, and its slicing respects a caller-supplied window (slices
  inside it).
- `health_agent/literature/fetch/refresh.py` (new): `window_for(pack_row,
  since, today)`, `refresh_pack(conn, spec, *, since, today, get, sleep,
  progress) -> RefreshStats(window_from, window_to, matched, added,
  retracted)`. Pure orchestration over eutils and corpus; no CLI, no
  printing.
- `health_agent/cli.py`: `cmd_literature_refresh`, parser entry; status,
  packs, check lines; posture text.
- `health_agent/consent.py`: notice version 2.
- `THREAT_MODEL.md`, `README.md`, `ROADMAP.md`.

## 8. Testing

- Schema: a v4 file opens, is migrated, reads as 5, keeps its rows; a v3
  file still refuses.
- `corpus.add`: adds new PMIDs, upserts an existing one, links both to the
  pack, unlinks nothing (an article linked before and absent from the
  additions stays linked), writes `refresh_log`, sets `refreshed_at`,
  leaves `version` and `built_at` alone. `mark_retracted` flips only rows
  that exist and returns the count.
- `eutils.search` sends `datetype=edat` with the window; `fetch_term`
  with a window slices inside it.
- `refresh_pack` with a fake NCBI: two esearch calls per pack (additions
  with the window, retractions without), the right term in each, the
  stats match what the fake served; a fetch failure raises before
  anything is written (the corpus is unchanged).
- CLI: `refresh` with `eutils` monkeypatched adds articles and prints the
  counts; `refresh sample` prints the snapshot line and exits 0; an
  uninstalled slug exits 2 before any request; `--since` shows in the
  printed window; consent version 1 on disk re-asks (or `--yes` records
  version 2); `--no-embed` skips embedding.
- `test_no_network.py`: `refresh.py` lives under `fetch/` and imports no
  transport itself; `cmd_literature_refresh` imports `fetch` only inside
  the function, like `install`; the offline pipeline test still passes.
- `consent`: the notice text names `refresh`; version is 2; a v1 record
  needs a prompt.

Not in scope: scheduling, a "refresh due" nag, refreshing `sample`,
removing articles that have dropped out of a query, and refreshing from a
newer release version (that stays `install`).
