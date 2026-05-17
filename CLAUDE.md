# CLAUDE.md

Guidance for AI assistants working on this repo. Read this before changing
anything in `scanner/`, `site/`, or `.github/workflows/`.

## What this is

Static personal gallery of 3D-printable miniatures the user buys from
**NomNom Figures** (Patreon, monthly drops). Each release lands in their
Google Drive as a shared folder which the user adds to their own Drive
as a shortcut. Over months that turns into dozens of folders with
inconsistent internal layouts — some have STLs at the top level, some
nest them under `STL/Bust/`, `Presupports/STL/`, `1/10 Scale Split/`,
or pack everything inside a `_STL.7z` archive. Cover renders sit
sometimes next to the STLs, sometimes in `Render Images/`, sometimes
in the parent folder, sometimes named `Beauty shot.jpg`, sometimes
`BS 01.jpg`, sometimes `Triss.jpg`, sometimes just `12.jpg`.

The goal is a browsable catalogue — open a phone, see every model the
user owns as a card with a painted-figure preview, tap through to the
Drive page when they want to print one. Not a store, not a viewer —
an index.

### Who it's for

One user (the repo owner). Public-readable on GitHub Pages because
GitHub Pages on a free account is public, but everything points back
into the user's Drive — anyone clicking a card lands on Drive's own
permission check. STL bytes are never copied into the repo, never
re-uploaded anywhere, never thumbnailed beyond a 600px JPEG of the
*image* (not the model). NomNom owns the models; this project is a
client-side index for files the user already has rights to.

### Why this shape

- **Static site on GitHub Pages**: the user wanted zero infrastructure
  — no server, no DB, no CDN bill. The whole runtime budget is the
  GitHub Actions free tier.
- **Daily cron + manual dispatch**: NomNom drops monthly, so once a
  day is plenty. Manual dispatch covers the "I just bought another
  pack, refresh now" case.
- **Drive API rather than `gcloud` / `gws` CLI**: the user's first
  instinct was a CLI, but file-level Drive operations are squarely a
  Drive API job. Service accounts can't see files shared with the
  user's personal Gmail (different identity), so auth defaults to
  user OAuth refresh token; an API key path exists for fully public
  trees because it's two minutes of setup vs ten.
- **Cover image picked by *colourfulness*, not size or alphabet**:
  NomNom ships ~10–30 renders per character — painted hero shots,
  greyscale STL renders, scale charts, parts breakdowns. Painted
  figures dominate Hasler-Süsstrunk colorfulness; greyscale technical
  sheets bottom out near zero. Filename heuristics layer on top to
  short-circuit the obvious cases (`Beauty shot.jpg`, `BS 01.jpg`,
  `Geralt.jpg`-in-Geralt-folder).

### What "done" looks like

- Open https://arturskowronski.github.io/stl-gdrive-viewer/ on a phone.
- Single column, painted-figure thumbnails, character name + release
  chip, search box, release filter.
- Each card has one button per STL/archive in that character's folder
  (presupported variants first), opening Drive's web view.
- Daily Actions run takes ~1–2 minutes, costs ~5–10 Actions minutes,
  redeploys Pages without manual intervention.

### Live UI strings are Polish

UI strings are Polish; comments and identifiers are English. Mirror
that when adding code or copy.

## Architecture

```
Google Drive
    │  (Drive API v3 — API key OR OAuth refresh token, auto-detected)
    ▼
scanner/
    drive.py          thin client, throttle + retry, thumbnailLink fast path
    walker.py         tree → list[Model] (generic-folder collapse, group/release labels)
    selector.py       Model.image_candidates → ScoredImage (cover decision)
    thumbs.py         Pillow → 600px JPEG, deterministic filename
    telegram.py       optional: scrape t.me/s/<channel> public widget HTML
    scan.py           orchestrator + manifest writer + --analyze CSV mode
    │
    ▼
site/
    manifest.json     {generated_at, releases[], models[{id,name,release,thumb,source?,stls[]}]}
    thumbs/*.jpg      generated, .gitignored
    index.html, app.js, styles.css   vanilla, no build step
    │
    ▼
GitHub Pages
```

Workflows in `.github/workflows/`:
- `refresh.yml` — push to main, daily cron, manual dispatch. Builds & deploys.
  Has `analyze: true` toggle that produces `cover-analysis.csv` artifact instead.
- `test.yml` — pytest on every push and PR.

## Cover selection logic — the most important thing

Five layers, evaluated in order. Each layer that finds a match returns
that file; layers below never run. **Tests in `tests/test_selector.py`
freeze every rule below — change them on purpose, never accidentally.**

1. **Primary hard short-circuit** (`_is_hard_pick`):
   - `Beauty shot.jpg` / `BeautyShot_01.png` / `Beauty_Pic.jpg`
   - `Foo BS 01.jpg` (NomNom's "BS NN" abbreviation, only when "BS" is
     not preceded by a letter — `ABS_engine.jpg` does NOT match)
   - `FinalRender.jpg` / `Final_Render.jpg` (any separator)
   - `Final.jpg` (bare, entire base name)
   - `FolderName.jpg` — filename is a single capitalised proper-noun
     token of the model folder name (`Geralt.jpg` in "Geralt from God of War")

2. **Secondary hard short-circuit** (`_is_secondary_pick`) — only if
   primary is empty:
   - `cover.jpg`, `Foo_Cover.jpg`
   - `Poster.jpg`, `Poster_01.jpg`

   Both regexes refuse a preceding letter (no `BookCover`, no `WallPoster`).

3. **Hint pool** narrows scoring (`_has_hint`):
   - filename contains `final` or `render` as a word
   - filename is a clean single proper noun (Triss.jpg)
   - filename shares a non-stopword token with the folder name

   If any candidates match, scoring runs **only on those**. If none, scoring
   runs on all candidates.

4. **Colourfulness scoring** (`score_image_bytes`):
   `0.7 * Hasler-Süsstrunk colorfulness + 0.3 * mean HSV saturation`,
   computed on a 256px downscale. Painted minis ~1.5, greyscale renders ~0.05.
   Robust separator — DO NOT replace with size-based or filename-based
   tiebreakers; that regressed three different times.

5. **Fallback**: first successfully-decoded image when scoring throws.

**Within a tier**, ordering is `(_series_number(name), -file_size)` —
lowest number wins (BS 01 beats BS 02), file size as tiebreaker. The
`_series_number` is the LAST integer in the filename.

`MAX_SCORED_PER_MODEL = 6` caps the scoring pool to keep Drive API load
bounded. We sort by file size desc before truncating.

## Walker rules

`scanner/walker.py` classifies each folder during a post-order DFS:

- **Generic name** = every token is in `GENERIC_TOKENS` (`stl`, `bust`,
  `split`, `presupported`, `unsupported`, `scale`, `miniature`, `mm`,
  `render`, `images`, ...) or pure digits. `75mm`, `1/10 Scale Split`,
  `Presupports`, `STL` are all generic. `AhsokaTano`, `Captain America`,
  `TifaBust` are not.

- **Model** = non-generic folder whose subtree contains ≥1 STL **or
  archive** (`.7z`/`.zip`/`.rar`) and which has no non-generic descendant
  that's also a model. Aggregates all STLs and images from its subtree.

- **Group (release)** = non-generic folder whose subtree contains models.
  Its name labels the `release` field on those models. **Group images
  are distributed to children only when all children share the same
  `display_name`** — Kratos_STL + Kratos_Presupport (both display as
  "Kratos") receive the parent's BeautyShot, but multi-character
  releases like "April 2026 Lootbox Release" don't smear their promo
  across distinct child characters.

- **Trailing format suffix** stripped from display name: `_STL`, `_Bust`,
  `_Split`, `_Presupport`, etc. `Asuka_STL` → display `Asuka`.

- **Renders-only sibling folders** (`render images/`, no STLs of their
  own) bubble their images upward. The model folder above collects them.

`Model.name` is the raw Drive folder name (used in logs and thumbnail
filenames — stable across heuristic changes). `Model.display_name` is
the cleaned label that goes into the manifest.

After walker returns, `scan.py` merges models with the same
`(release, display_name)` and dedupes image and STL lists by file id.

## Drive API guardrails

- **Auth auto-detect**: `GOOGLE_API_KEY` is preferred; OAuth refresh
  token (`GOOGLE_OAUTH_CLIENT_ID/_SECRET/_REFRESH_TOKEN`) is the fallback.
  Service accounts are NOT supported — they can't see files shared with
  the user's personal Gmail.

- **Thumbnail fast path**: `DriveClient.fetch_thumbnail(file)` hits
  `lh3.googleusercontent.com` directly with a token-bearing URL from
  `thumbnailLink`. Bypasses API quota entirely. Use this for cover
  fetching (selector does so via `_fetch_image`); fall back to
  `download_bytes` only when thumbnailLink is unavailable.

- **Throttle + retry**: `DriveClient` enforces 0.3s between requests
  and retries 403/429/5xx with exponential backoff (up to 5 attempts).
  When you see Google's "We're sorry... your network may be sending
  automated queries" HTML, raise the throttle, don't loosen the retry.

- **Read-only**: scope is `drive.readonly`. Never write, rename, or
  reorganise Drive content. The renaming heuristics live entirely in
  `_meaningful_name` and only affect the manifest.

## STL files

**We do not redistribute STLs.** Cards link to `webViewLink` (the Drive
page) so the file's existing permissions decide whether the viewer can
download. Public files behave like a download link; private files prompt
for login. This is intentional licence-wise (NomNom owns the models).

Archives (`.7z`/`.zip`/`.rar`) and pre-sliced resin formats
(`.ctb` ChituBox, `.goo` Elegoo native) count as "model files" alongside
`.stl`. Each model exposes them through a single `<select>` dropdown
plus a "Pobierz" button — presupported variants are tagged with `★`,
Saturn-4-Ultra-optimized files with `[Saturn]`. A separate
`Folder na Drive ↗` link covers the "give me everything" case. Within
the dropdown, presupported variants come first, then largest first.

Semi-product files (`test`, `sample`, `demo`, `preview`, `WIP`,
`calibration`, `cut_test`, `stress_test`, `temple`, `benchmark`,
`bench_print` as standalone tokens) are stripped from the per-card
list — those are tooling, not figures. Filter lives in
`selector._is_semi_product_stl`; if every STL in a model matches, the
filter is bypassed so we never empty out a card.

## Saturn 4 Ultra detection

`scanner/selector._is_saturn_optimized(filename, parent_chain)` flags
files that target the Elegoo Saturn 4 Ultra specifically. The regex is
intentionally strict — generic "Saturn", "Elegoo", "12K", "ChituBox"
all match too many printers and would create false positives:

  - Match: `Saturn 4 Ultra` (any separator), `S4U`, `EL-3D-S4U`
  - No match: `Saturn 3 Ultra`, bare `Saturn`, `Mars 4 Ultra`, `12K`,
    `Elegoo`, `ChituBox profile`
  - No substring matches: lookarounds use `[A-Za-z0-9]` (not `\b`,
    which treats `_` as a word char) so `S4U_Presupported.stl` matches
    but `TrissS4Ultra.stl` and `AlbatrossS4U.stl` don't.

The detector consults the file's full ancestor chain
(`StlEntry.parent_chain`), not just the immediate parent — a marker on
`Saturn 4 Ultra/Presupports/STL/foo.stl` propagates to the file even
though the immediate parent is just `STL`.

Manifest exposes `saturn_optimized: bool` per STL and per model; the
frontend uses it to render a `[Saturn]` prefix on dropdown options, an
amber `Saturn optimized` chip on the card, and a `Tylko Saturn 4 Ultra`
filter button in the toolbar (auto-hidden when the manifest contains
zero Saturn-flagged models). **Don't broaden the regex without a
test** — the same revert/redo cycle that bit the cover heuristics
applies here.

## Frontend

Vanilla HTML/CSS/JS. No bundler. `app.js` fetches `manifest.json`,
renders cards. CSS Grid with `auto-fill, minmax(min(260px, 100%), 1fr)`.

- Mobile (<720px): card image flows at natural height (full-bleed,
  no letterbox).
- Desktop (≥720px): fixed 4:3 aspect ratio with `object-fit: contain`
  for uniform alignment.

Polish plural forms (`plPlural`) handled correctly: 1 model / 2-4
modele / 5+ modeli.

Cards without a thumbnail (manifest `thumb: null`) render a gradient
initial-letter placeholder — STL link still works.

## Testing

```bash
pip install -r tests/requirements.txt
python -m pytest tests/ -v
```

100 tests, ~3s, no network, drive client stubbed. Two files:
`tests/test_selector.py` (regex tiers, hint pool, series number) and
`tests/test_walker.py` (generic-folder collapse, image distribution,
synthetic Drive trees mirroring real NomNom structures).

CI runs the same suite on every push to main and every PR.

## Common pitfalls

- **Don't add filename-based hard short-circuits beyond the existing set
  without a test case.** Each one we added (final, render, folder-name
  match, proper-noun) caused a regression where a technical PARTS / SCALE
  sheet matching the pattern was picked over the painted figure. The
  current set is the result of multiple revert/redo cycles — extend it
  via tests, not via on-the-fly tweaks.

- **Don't use file size as the only tiebreaker in scoring.** It picks
  the largest technical reference sheet over the painted mini. Use
  colourfulness; size only as the very last resort within an explicit
  hard-pick tier.

- **Don't introduce `cryptography` indirectly into selector tests.**
  The test stub for `scanner.drive` is what keeps the suite running
  without google-api-python-client and its transitive deps. If you
  need to test something that requires the real client, add a separate
  test module that's allowed to be slow.

- **Don't break the `_is_generic_name` invariant**: it must return True
  if every token is generic. Adding new generic tokens is fine; making
  it stricter (e.g. requiring N tokens) breaks the Inuyasha-collapse test.

- **Don't change the manifest schema casually**: `app.js` reads
  `models[].name`, `release`, `thumb`, `folder_url`, `stls[].view_url`,
  `stls[].name`, `stls[].size`, `stls[].presupported`. Anything else
  is internal to scan.py.

## Telegram → Drive bot (`bot/`)

Optional companion service. Long-running Python process the user
deploys somewhere with Docker (homeserver, Fly.io, Railway, RPi).
Listens for messages forwarded into a private chat with a Telegram
bot, downloads the document via a self-hosted Telegram Bot API
server (lifts the 20 MB public-API getFile cap so multi-GB archives
work), uploads it to a sub-folder under the same `DRIVE_ROOT_FOLDER_ID`
the scanner indexes, then replies with a Drive URL.

No new code path on the gallery side — uploads land in the same Drive
root, the next `Refresh gallery` cron picks them up like any other
new model folder. The bot is just an alternate ingest mechanism for
content the user wants in their personal Drive without manually
downloading + re-uploading.

Setup is one-time and documented in `bot/README.md`. Highlights:

  - `python-telegram-bot==21.6` configured against a local TBA server
    (`aiogram/telegram-bot-api` Docker image)
  - shared Docker volume between TBA and bot, so multi-GB downloaded
    files land on disk once and the bot reads them directly instead of
    re-downloading through HTTPS
  - `ALLOWED_USER_IDS` env var (comma-separated Telegram user IDs)
    gates who can upload — uploads land in YOUR Drive, so default-deny
  - `scanner/auth_bootstrap.py --write` mints a new OAuth refresh
    token with the full `drive` scope; the scanner keeps its readonly
    token, the bot uses the write one
  - bot is idempotent on `(folder_name, filename)` — re-forwarding
    the same archive replies "already there" without re-uploading

`bot/worker.py` buffers media-group messages by `media_group_id` with
a 2 s flush window (Telegram delivers album + document as separate
updates), pairs the first photo with the first model-extension
document, and queues a single upload per group.

## Telegram as a second source

Optional. Enabled by setting the `TELEGRAM_CHANNEL` repository variable
(Settings → Secrets and variables → Actions → Variables) to a public
channel username — no `@`, no `t.me/` prefix, e.g. `Best_STL_3D`. Leave
the variable unset to skip Telegram entirely.

`scanner/telegram.py` scrapes only the **first page** of
`t.me/s/<channel>` on every run. The same incremental contract that
protects Drive ("once indexed, doesn't change") applies here:

  - new posts on the first page get fetched, thumbnailed, written to
    the manifest with id `tg:<channel>:<message_id>`
  - already-known message ids are skipped via the cached manifest
  - posts that scrolled off the first page are carried forward from
    cache verbatim — we deliberately never paginate deep history, so
    the channel is indexed forward-only from the moment we first
    started watching

A "model" = one **document message** (`.rar/.zip/.7z/.stl/.ctb/.goo`)
plus the immediately preceding photo-only message as its cover (Telegram
media groups: an album of images followed by a file, posted as one
unit). Cover URL is the inline `background-image` of the widget's
photo wrap; downloaded as a few-hundred-KB JPEG from the CDN exactly
like Drive's thumbnailLink fast path.

Frontend distinguishes Telegram-sourced cards with a small blue `TG`
badge on the card header and a `Otwórz w Telegramie ↗` folder link
instead of the Drive one. STL dropdown logic is unchanged — `view_url`
is just a `t.me/<channel>/<msg_id>` deep link that opens in the user's
Telegram client.

The Sunday rebuild also runs the Telegram pass (same first-page,
lightweight code path) — it doesn't deep-walk the channel either,
because doing so would multiply HTTP requests + scraping fragility
for no real benefit. Existing TG entries survive the Sunday rebuild
through cache carry-forward; see `rebuild.yml`'s "Drop Drive-source
state" step.

The scraper relies on Telegram's public widget HTML structure
(`tgme_widget_message`, `tgme_widget_message_photo_wrap`,
`tgme_widget_message_document`). If those classes ever change,
`tests/test_telegram.py` will keep passing (the test uses a fixed
snapshot) but the live run will silently return zero models — log a
"telegram: parse failed" or "0 model(s) on first page" warning. Update
the selectors in `scanner/telegram.py::parse_page` and the snapshot
HTML in the test together.

**Licence stance, same as Drive.** STL bytes never enter the repo;
Telegram document messages are referenced by URL only, like Drive's
`webViewLink`. The viewer's existing Telegram permissions decide
whether they can download. The channel is expected to be the user's
own — content they have rights to — the same way the Drive root is.

## Incremental vs full scans

Two workflows feed the same Pages deployment:

- **`Refresh gallery`** (`refresh.yml`) — Mon–Sat 02:00 UTC cron, plus
  push to main and manual dispatch. Runs `scanner --incremental`: every
  model whose `folder_id` already appears in the cached manifest is
  copied forward verbatim (no Drive image fetch, no PIL pass), only
  new models pay the cost. State persists across runs via `actions/cache`
  keyed `gallery-state-*`. Orphan thumb files (model deleted from
  Drive) are pruned each run. **Important:** the walker still fully
  descends the tree every run — only the per-model `pick_cover` and
  thumb generation are skipped — so new model subfolders that
  appear inside an already-known release folder mid-month (e.g.
  NomNom dropping the third character of "April 2026 Release" two
  weeks in) are detected as new `folder_id`s and processed fully on
  the next nightly run.

- **`Rebuild gallery from scratch`** (`rebuild.yml`) — Sunday 02:00 UTC
  cron and manual dispatch. Drops the cached `site/manifest.json` +
  `site/thumbs/`, runs the scanner with no `--incremental` flag,
  re-scores every cover and re-picks every STL list. Fresh state is
  saved back to the same cache key so Monday's refresh resumes from
  this baseline. The weekly cron exists so changes to existing models
  (new BS render, new presupported variant added to a folder we've
  already indexed) are picked up automatically once a week without
  needing manual intervention; manual dispatch covers the
  "I just changed selector heuristics, want it now" case.

The contract: incremental trusts that **once a folder_id is indexed,
its contents don't change**. STL renames inside an existing model
folder, new image candidates, presupported additions — none of those
are noticed until a full rebuild. NomNom's release model (one drop
per month per character, then frozen) makes this safe in practice.

Helpers live in `scanner/scan.py`:

  - `_load_existing_manifest(out_path)` → `{folder_id: entry}` or `{}`
    on missing/unparseable file (caller falls back to full scan)
  - `_prune_orphan_thumbs(thumbs_dir, models)` removes thumb files no
    longer referenced by any manifest entry, returns the count

## Branch and deploy

- Default branch: `main`. The earlier `claude/model-gallery-google-drive-3gVLK`
  branch was renamed; that name no longer exists.
- Deploys are environment `github-pages`. If a "Failed after 1s — no
  steps" appears on the deploy job, it's the environment's branch
  protection — check Settings → Environments → github-pages → deployment
  branches.
- The `analyze: true` workflow input runs the scanner in audit mode and
  uploads `cover-analysis.csv` instead of deploying. Use this when you
  want to see exactly which file each model would pick and why, without
  affecting the live gallery.

## Files quick map

| Path | What it does |
|---|---|
| `scanner/drive.py` | Drive API wrapper, throttle/retry, thumbnailLink, OAuth+API-key auth |
| `scanner/walker.py` | tree → models, generic-folder collapse, display rename |
| `scanner/selector.py` | cover regex tiers, scoring, hint pool |
| `scanner/telegram.py` | optional second source: scrape t.me/s/&lt;channel&gt; first page |
| `scanner/scan.py` | CLI entrypoint, manifest writer, `--analyze` CSV |
| `scanner/thumbs.py` | Pillow thumbnail generation |
| `scanner/auth_bootstrap.py` | one-time local script to mint OAuth refresh token (readonly default, `--write` for bot) |
| `bot/worker.py` | Telegram bot: receives forwarded archives, uploads to Drive |
| `bot/drive_writer.py` | Drive write helper used by the bot — folder create + resumable file upload |
| `bot/docker-compose.yml` | runs TBA server + bot worker together |
| `site/app.js` | manifest fetch, card render, search, release filter |
| `site/styles.css` | grid, dark mode, mobile-first responsive |
| `tests/test_selector.py` | 60 frozen rules for cover regex + scoring |
| `tests/test_walker.py` | 40 frozen rules for tree classification |
| `.github/workflows/refresh.yml` | scan + deploy + cron |
| `.github/workflows/test.yml` | pytest on push/PR |
