<p align="center">
  <img src="frontend/public/mangarr-icon.svg" width="160" height="160" alt="Mangarr icon">
</p>

# mangarr

Radarr/Sonarr-style automation for manga. Monitor series, automatically grab
new chapters from direct sources or torrents, and organize everything as CBZ
files with `ComicInfo.xml` — ready for [Komga](https://komga.org) or
[Kavita](https://www.kavitareader.com). Mangarr has no built-in reader by
design; it is the automation half of your manga stack.

![stack](https://img.shields.io/badge/backend-FastAPI-009688) ![stack](https://img.shields.io/badge/frontend-React-61dafb) ![stack](https://img.shields.io/badge/db-SQLite-003b57)

## Features

- **Library management** — add series via MangaUpdates metadata search
  (covers, descriptions, status; AniList remains available as a fallback
  provider), poster-grid library, per-series chapter tables with monitor
  toggles, wanted/missing view. MangaUpdates tracks scanlation releases per
  chapter, so chapter/volume counts stay current for ongoing series where
  AniList lags, and its release feed fills in chapters no direct source
  serves yet.
- **Sources** — grabbed in a configurable priority order (fast scanlation
  sources first, archive sources as fallback):
  - **MangaPlus** (Shueisha, official) — the true same-day source for Shonen
    Jump titles (One Piece, Kagurabachi, Dandadan, Jujutsu Kaisen…). Only the
    first and latest few chapters of each title are free, which is exactly the
    new-chapter use case. **Off by default**: the API bans datacenter IPs, so
    it only works from a residential IP (e.g. a home server). Enable it in
    Settings once you've confirmed it reaches the API from your host.
  - **TCB Scans** (scraper) — fastest scanlations of the big Jump titles,
    usually within hours. Small catalog of major series only.
  - **MangaDex** (API) — huge, well-tagged archive; supplies volume data. Works
    anonymously but limits guests to 10 chapters/day — add a free account +
    personal API client in Settings for normal use.
  - **MangaFire** (API) — broad English archive and an additional direct source
    for decimal-numbered bonus and special chapters.
  - **WeebCentral** (scraper) — broad catalog, no account needed.
  - **Asura Scans** (API) — Korean/Chinese webtoons and manhwa (not Japanese
    manga). Skips locked early-access (premium) chapters automatically.
  - **Nyaa.si** (torrents) — Literature/English-translated category, sent to
    qBittorrent; completed downloads are imported automatically (volume packs,
    chapter archives, or loose-image folders). Imports **hardlink** by
    default — the torrent keeps seeding and the library copy costs no extra
    space (downloads and library must share a filesystem; falls back to copy
    automatically, and a copy mode setting is available).
- **Automation** — a monitor loop checks linked sources for new chapters of
  monitored series and grabs them by configurable source priority. Manual
  per-chapter interactive search included.
- **Output** — one CBZ per chapter, `ComicInfo.xml` embedded, Komga/Kavita
  naming: `Series Title/Series Title - Vol. 01 Ch. 0021.cbz`.
- ***arr-style API** — everything under `/api/v1` with `X-Api-Key` auth.

## Quick start (Docker)

```bash
git clone <this repo> mangarr && cd mangarr
docker compose up -d
```

The default Compose file runs the published `kirantheram/mangarr:latest`
Docker Hub image and stores configuration and media under `./data`. Open
<http://localhost:6996> after it starts.

First-run checklist, in the mangarr UI:

1. **Settings → Root Folders**: add `/media/manga`.
2. **Settings → MangaDex Account** (recommended): create a free account at
   mangadex.org, then *Settings → API Clients* there to make a personal
   client; paste client id/secret and your username/password.
3. **Add New**: search a title, pick a root folder, add. Chapters appear after
   the automatic source-linking pass (a few seconds). If **Search now** and
   qBittorrent are enabled, Mangarr also inspects Nyaa torrent file lists,
   queues the seeded release with the most missing-chapter coverage, then uses
   direct sources for anything that release does not contain. Automatic
   torrents are capped at 30 GiB by default and can be tuned in Settings.

### Using an existing qBittorrent container

Mangarr does not bundle qBittorrent. To enable torrent downloads, connect it
to a qBittorrent instance you already run:

1. Mount the same host media directory into both containers at the same
   container path. With the default Compose file, mount `./data/media` in
   qBittorrent as `/media` too. On a NAS or unRAID system, both containers
   could instead use a shared mapping such as `/mnt/user/media:/media`.
2. Make qBittorrent reachable from Mangarr. Put both containers on the same
   Docker network and use a URL such as `http://qbittorrent:8080`, or use the
   qBittorrent host's LAN address and WebUI port.
3. In **Settings → Download Client**, enter that URL and the qBittorrent WebUI
   credentials, then select **Test Connection**.
4. Set **Downloads folder** to `/media/torrents`. Because qBittorrent and
   Mangarr see the same path, Mangarr can import completed downloads and
   hardlink them into `/media/manga` without duplicating data while the
   torrent continues seeding.

## Using an existing library

Mangarr can sit on top of a library you already have and adopt it in place —
it won't re-download what's on disk.

1. **Mount your library** into the mangarr container and add it as a root
   folder. In `docker-compose.yml`, add a volume (e.g.
   `- /mnt/nas/manga:/library`), then Settings → Root Folders → add `/library`.
2. **Add each series** as usual. On add (and on every refresh) mangarr finds the
   matching folder in the root, scans it, and marks chapters/volumes you already
   have as owned. The Wanted list then shows only what's genuinely missing.
3. **Per-series tools** (on the series page):
   - **Scan Disk** — re-scan the folder and adopt any new files.
   - **Files** — see everything found, with unmatched files you can map to a
     chapter by hand.
   - **Rename** — preview and apply mangarr's naming convention
     (`Series - Vol. 01 Ch. 0021.cbz`). Renaming **preserves the original
     format** (a `.cbr` stays `.cbr`), never overwrites an existing file, and
     never deletes anything.
   - **Change folder** — browse the filesystem to point a series at its folder,
     including a subfolder for collections (e.g. `Attack On Titan/Attack On
     Titan`).

Matching is filename-based and handles the usual variety: `Series - Ch. 12.cbz`,
`Series ch. 12`, `Volume 03.cbr`, `Series v03 (2019).cbz`, whole-volume archives
(which mark every chapter in that volume), and folders of loose images.

## Local development

Backend (Python ≥3.11):

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn mangarr.main:app --port 6996 --reload
```

Frontend (Node ≥20):

```bash
cd frontend
npm install
npm run dev        # Vite dev server on :5173, proxies API to :6996
```

Tests:

```bash
cd backend && .venv/bin/python -m pytest
```

`npm run build` writes the production bundle to `backend/static/`, which the
FastAPI app serves when present.

## Configuration

Environment variables (all optional):

| Variable            | Default | Description                          |
| ------------------- | ------- | ------------------------------------ |
| `MANGARR_PORT`      | `6996`  | HTTP port                            |
| `MANGARR_DATA_DIR`  | `data`  | SQLite DB, API key, cached settings  |

Everything else (sources, credentials, naming templates, qBittorrent,
monitor interval) lives in the UI under Settings and is stored in the DB.

The web UI's API key is generated on first start at `<data dir>/api_key` and
handed to the UI via `GET /initialize.json`. To give external clients (e.g.
NextPanel or scripts) their own keys, create named keys under **Settings → API
Keys**; any of them authenticates `/api/v1` calls via `X-Api-Key` and can be
revoked independently.

## How grabbing works

1. When you add a series, mangarr links it to each enabled source by title
   (including MangaUpdates associated titles). Links are per-source, so a
   site changing its layout breaks one source, never the app.
2. The monitor job (default: every 15 min) diffs source chapter lists against
   the library. New monitored, missing chapters are grabbed from the highest
   priority source that has them (`Settings → Sources → priority`).
3. Direct grabs download pages with per-source rate limits and pack them into
   a CBZ. Add-time torrent searches inspect the small `.torrent` metadata
   first and rank releases by exact file coverage, special-chapter coverage,
   seeders, and size. The selected release goes to qBittorrent under the
   `mangarr` category and is imported when complete.

Please be a good citizen: keep the honest User-Agent, don't lower the rate
limits, and use a MangaDex account so their team can see the traffic is
legitimate.

## Roadmap

- Western comics support (ComicVine metadata + GetComics source) — the
  source/metadata plugin interfaces are already in place.
- Notifications (Discord/webhooks) on grab/import.
