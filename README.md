# mangarr

Radarr/Sonarr-style automation for manga. Monitor series, automatically grab
new chapters from direct sources or torrents, and organize everything as CBZ
files with `ComicInfo.xml` — ready for [Komga](https://komga.org) or
[Kavita](https://www.kavitareader.com). Mangarr has no built-in reader by
design; it is the automation half of your manga stack.

![stack](https://img.shields.io/badge/backend-FastAPI-009688) ![stack](https://img.shields.io/badge/frontend-React-61dafb) ![stack](https://img.shields.io/badge/db-SQLite-003b57)

## Features

- **Library management** — add series via AniList metadata search (covers,
  descriptions, status), poster-grid library, per-series chapter tables with
  monitor toggles, wanted/missing view.
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
  - **WeebCentral** (scraper) — broad catalog, no account needed.
  - **Asura Scans** (API) — Korean/Chinese webtoons and manhwa (not Japanese
    manga). Skips locked early-access (premium) chapters automatically.
  - **Nyaa.si** (torrents) — Literature/English-translated category, sent to
    qBittorrent; completed downloads are imported automatically (volume packs,
    chapter archives, or loose-image folders).
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

Open <http://localhost:6996>. The compose file also starts a
[linuxserver/qbittorrent](https://docs.linuxserver.io/images/docker-qbittorrent/)
container on <http://localhost:8080> sharing a `./data/downloads` volume with
mangarr — the shared mount is what makes torrent import work (mangarr must see
completed downloads at the same path qBittorrent reports).

First-run checklist, in the mangarr UI:

1. **Settings → Root Folders**: add `/manga` (mapped to `./data/manga`).
2. **Settings → Download Client**: qBittorrent URL `http://qbittorrent:8080`
   plus the WebUI credentials (check the qbittorrent container logs for the
   temporary password on first boot), then *Test Connection*.
3. **Settings → MangaDex Account** (recommended): create a free account at
   mangadex.org, then *Settings → API Clients* there to make a personal
   client; paste client id/secret and your username/password.
4. **Add New**: search a title, pick a root folder, add. Chapters appear after
   the automatic source-linking pass (a few seconds).

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

The API key is generated on first start at `<data dir>/api_key` and shown by
`GET /initialize.json`.

## How grabbing works

1. When you add a series, mangarr links it to each enabled source by title
   (including AniList alt titles). Links are per-source, so a site changing
   its layout breaks one source, never the app.
2. The monitor job (default: every 15 min) diffs source chapter lists against
   the library. New monitored, missing chapters are grabbed from the highest
   priority source that has them (`Settings → Sources → priority`).
3. Direct grabs download pages with per-source rate limits (MangaDex: 5 req/s
   API, 40 img/min) and pack them into a CBZ. Torrent grabs go to qBittorrent
   under the `mangarr` category and are imported when complete.

Please be a good citizen: keep the honest User-Agent, don't lower the rate
limits, and use a MangaDex account so their team can see the traffic is
legitimate.

## Roadmap

- Western comics support (ComicVine metadata + GetComics source) — the
  source/metadata plugin interfaces are already in place.
- MangaUpdates metadata for better chapter-count data.
- Notifications (Discord/webhooks) on grab/import.
