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
    scan.py           orchestrator + manifest writer + --analyze CSV mode
    │
    ▼
site/
    manifest.json     {generated_at, releases[], models[{id,name,release,thumb,stls[]}]}
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
| `scanner/scan.py` | CLI entrypoint, manifest writer, `--analyze` CSV |
| `scanner/thumbs.py` | Pillow thumbnail generation |
| `scanner/auth_bootstrap.py` | one-time local script to mint OAuth refresh token |
| `site/app.js` | manifest fetch, card render, search, release filter |
| `site/styles.css` | grid, dark mode, mobile-first responsive |
| `tests/test_selector.py` | 60 frozen rules for cover regex + scoring |
| `tests/test_walker.py` | 40 frozen rules for tree classification |
| `.github/workflows/refresh.yml` | scan + deploy + cron |
| `.github/workflows/test.yml` | pytest on push/PR |
