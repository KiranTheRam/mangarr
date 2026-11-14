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
  - **WEBTOON** (official) — English WEBTOON Originals. Only episodes that
    are currently free are listed (coin-locked Daily Pass / Fast Pass
    episodes are skipped), so it's most useful for ongoing series.
  - **TCB Scans** (scraper) — fastest scanlations of the big Jump titles,
    usually within hours. Small catalog of major series only.
  - **MangaDex** (API) — huge, well-tagged archive; supplies volume data. Works
    anonymously but limits guests to 10 chapters/day — add a free account +
    personal API client in Settings for normal use.
  - **MangaFire** (API) — broad English archive and an additional direct source
    for decimal-numbered bonus and special chapters.
  - **FanFox** (scraper, opt-in) — public mobile reader, including numbered extras.
    Mangarr checks the public reader and its first image before offering a release.
  - **WeebCentral** (scraper) — broad catalog, no account needed.
  - **Atsumaru** (API) — broad archive of manga, manhwa, and manhua. Where
    several groups translated a chapter, the best-rated one is used. Recent
    uploads are AVIF images, which Kavita and Komga read natively.
  - **Asura Scans** (API) — Korean/Chinese webtoons and manhwa (not Japanese
    manga). Skips locked early-access (premium) chapters automatically.
  - **Nyaa.si** (torrents) — Literature/English-translated category, sent to
    qBittorrent; completed downloads are imported automatically (volume packs,
    chapter archives, or loose-image folders). Imports **hardlink** by
    default — the torrent keeps seeding and the library copy costs no extra
    space (downloads and library must share a filesystem; falls back to copy
    automatically, and a copy mode setting is available).
- **Discovery** — import lists follow AniList, MyAnimeList, MangaDex or
  MangaUpdates reading lists and add new entries automatically; a calendar
  shows recent releases and when each ongoing series' next chapter is due
  (from its usual release rhythm); series pages list sequels, side stories
  and recommendations from AniList and MangaUpdates, one click from adding.
- **Automation** — a monitor loop checks linked sources for new chapters of
  monitored series and grabs them by configurable source priority. Manual
  per-chapter interactive search included.
- **Output** — one CBZ per chapter, `ComicInfo.xml` embedded, Komga/Kavita
  naming: `Series Title/Series Title - Vol. 01 Ch. 0021.cbz`.
- **Kavita scans** — optionally tell Kavita to scan after an import, so new
  chapters appear in the reader immediately. Scans the affected series when
  Kavita already knows it, and the whole library when it does not (a series
  mangarr just created).
- **Webhooks** — optionally POST a small JSON event to a URL of your choice
  whenever chapters are imported, so a request manager (e.g. NextPanel) sees
  them right away. Set it up under **Settings → Connect — Webhook**; an
  optional secret is sent as `X-Webhook-Secret`, and a dead endpoint never
  fails a download.
- ***arr-style API** — everything under `/api/v1` with `X-Api-Key` auth.

## Quick start (Docker)

```bash
git clone <this repo> mangarr && cd mangarr
docker compose up -d
```

The default Compose file runs the published `kirantheram/mangarr:latest`
Docker Hub image and stores configuration and media under `./data`. Open
<http://localhost:6996> after it starts.
Follow the setup screen to create a browser login.
The image's healthcheck polls `GET /ping` (no API key needed), so
`docker ps` shows the container as `healthy` once Mangarr is serving.

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

### Running as a non-root user (PUID / PGID / UMASK)

By default the container runs as root, so the files and folders Mangarr
creates are owned by root (files `0644`, folders `0755`). To have it create
them as your own user instead, for example so they stay editable over an SMB
share, set these on the container:

| Variable | Example | Description                                                |
| -------- | ------- | ---------------------------------------------------------- |
| `PUID`   | `99`    | User id to run as                                          |
| `PGID`   | `100`   | Group id to run as                                         |
| `UMASK`  | `002`   | umask for new files and folders (`002`: `664` / `775`)     |

Set `PUID` and `PGID` together; `UMASK` also works on its own. On unRAID,
`PUID=99` and `PGID=100` (`nobody:users`) are the usual choice, and
`UMASK=002` keeps new files group-writable. Leave them unset and nothing
changes.

On start, Mangarr makes its data directory (`/config`) owned by `PUID:PGID`
automatically. It never changes your library or download folders, so when
switching an existing install from root, give the new user those once, e.g.
on the host:

```bash
chown -R 99:100 /mnt/user/media/manga /mnt/user/media/torrents
```

Use your own root folder(s) and qBittorrent's downloads folder. Run
qBittorrent with the same `PUID`/`PGID` too, so Mangarr can still hardlink
the files it downloads.

### Triggering Kavita scans

Kavita only picks up new CBZ files when it scans. To have imports show up in
the reader immediately:

1. In Kavita, go to **Settings → Account** and copy the **API Key**.
2. In Mangarr, go to **Settings → Connect — Kavita**, turn it on, enter
   Kavita's URL (e.g. `http://kavita:5000`) and the API key, then select
   **Test Connection** — this also loads Kavita's libraries.
3. Check the **Library mapping** rows. Mangarr matches a root folder to a
   Kavita library by folder name, so `/media/manga` finds a library at
   `/data/manga` even when the containers mount it differently. Pick a library
   explicitly if the two paths have nothing in common.

**Scan scope** decides how much Kavita re-reads. *Series* scans only the
affected series folder, which is much cheaper on a large library. Kavita cannot
scan a series it has never indexed, so the first import for a newly added
series scans its whole library — as does a title Kavita holds more than once,
since guessing between them could scan the wrong one. *Whole library every
time* skips the lookup. Bursts of imports are batched into a single scan, and a
Kavita that is down or misconfigured never fails a download.

## Using an existing library

Mangarr can sit on top of a library you already have and adopt it in place —
it won't re-download what's on disk.

1. **Mount your library** into the mangarr container and add it as a root
   folder. In `docker-compose.yml`, add a volume (e.g.
   `- /mnt/nas/manga:/library`), then Settings → Root Folders → add `/library`.
2. **Import the folders**: Add New → **Library import** lists every folder in
   the root that no series uses yet, matches each to a MangaUpdates entry by
   name, and adds the ones you confirm in one go (each keeps its folder). You
   can also add series one at a time: on add (and on every refresh) mangarr
   finds the matching folder in the root. Either way it scans the folder and
   marks chapters/volumes you already have as owned, so the Wanted list shows
   only what's genuinely missing.
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
MANGARR_ALLOW_UNAUTHENTICATED_BROWSER=true .venv/bin/uvicorn mangarr.main:app --port 6996 --reload
```

Frontend (Node 20.19+ or 22.12+, as required by Vite 8):

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

## App versions

Docker Hub builds from `main` automatically increment the patch version for each
main-branch commit after the latest explicit version change. A merged PR counts
once when merged with a merge commit or squash merge; a rebase merge counts each
commit added to `main`. The version is calculated from Git history, so rerunning
the same build keeps the same version and needs no bot commits or write token.
The image's app/API version uses this calculated version. Existing `v*` tag
builds continue to use the tag as their version.

To choose a version manually, run this from the repository root:

```bash
python3 scripts/version.py set 1.2.0
```

Commit the resulting `backend/VERSION`, `frontend/package.json`, and
`frontend/package-lock.json` changes and merge them into `main`. That merge builds
as exactly `1.2.0`; subsequent main commits build as `1.2.1`, `1.2.2`, and so on.
Choose a version higher than the latest published version to keep versions
increasing. `backend/VERSION` is the release baseline; local installs use that
baseline unless a build supplies `MANGARR_BUILD_VERSION`. The frontend package
versions track the manually selected baseline.

Run `python3 scripts/version.py show` to inspect the version for `HEAD`, or add
`--revision <commit>` to inspect another commit. This requires a full Git clone;
CI checks out the complete history. Avoid rewriting `main` history, since the
commit count determines the patch increment.

Dependabot opens weekly update PRs for backend, frontend, Docker base images and GitHub Actions; they are merged by hand after review and CI.

## Configuration

Environment variables (all optional):

| Variable             | Default       | Description                                       |
| -------------------- | ------------- | ------------------------------------------------- |
| `MANGARR_HOST`       | `0.0.0.0`     | Address to listen on                              |
| `MANGARR_PORT`       | `6996`        | HTTP port                                         |
| `MANGARR_DATA_DIR`   | `data`        | SQLite DB, API key, cached settings               |
| `MANGARR_SQLITE_WAL` | `true`        | Use SQLite WAL mode (see below)                   |
| `MANGARR_LOG_LEVEL`  | `INFO`        | Log level (uppercase: `DEBUG`, `INFO`, `WARNING`) |
| `MANGARR_API_KEY`    | *(generated)* | Fixed web UI API key instead of a generated one   |
| `MANGARR_AUTH_USERNAME` | *(empty)* | Deployment-managed browser login username |
| `MANGARR_AUTH_PASSWORD` | *(empty)* | Browser login password; set together with username |
| `MANGARR_ALLOW_UNAUTHENTICATED_BROWSER` | `false` | Allow browser access without built-in login on an explicitly trusted deployment |
| `MANGARR_ALLOWED_HOSTS` | `[]` | Additional exact DNS names as a JSON array (no scheme, port or wildcard) |

`MANGARR_HOST` and `MANGARR_PORT` apply when Mangarr is started with
`python -m mangarr.main`; a `uvicorn` command line like the one under Local
development uses its own `--host`/`--port` flags instead.

**Database storage.** Mangarr runs SQLite in WAL mode, so next to
`mangarr.db` you will also see `mangarr.db-wal` and `mangarr.db-shm`. To back
up, stop the app and copy all three files, or use `sqlite3 mangarr.db ".backup
copy.db"`. WAL needs the config directory on local storage (a bind mount or
Docker volume on a local disk). If `/config` is on NFS or SMB, set
`MANGARR_SQLITE_WAL=false`; the database is then switched back to the
rollback journal on the next start.

Everything else (sources, credentials, naming templates, qBittorrent,
monitor interval) lives in the UI under Settings and is stored in the DB.

The web UI's API key is generated on first start at `<data dir>/api_key` (or
taken from `MANGARR_API_KEY` when set, in which case no file is written) and
handed to the UI via `GET /initialize.json`. To give external clients (e.g.
NextPanel or scripts) their own keys, create named keys under **Settings → API
Keys**; any of them authenticates `/api/v1` calls via `X-Api-Key` and can be
revoked independently.

### Browser access and upgrading

If Mangarr has no browser login, the first visit after installing or upgrading
opens a setup screen. Choose a username and password to create the login and
start using Mangarr immediately.
Complete this step on a trusted local network before exposing Mangarr through
a public reverse proxy or port forward. The first person who submits the form
creates the login. The library UI and browser API key stay behind the login
boundary during setup. Existing API-key integrations continue to work.

Use **Settings → Browser Security** to sign out or change an app-managed
password. A password change requires the current password and signs out all
browser sessions. API keys continue to work. Browser sessions expire after
seven days. After upgrading from an older release, sign in again to create a
session scoped to the instance's address and port.

Mangarr stores the password hash in `browser_login.json` in the data directory.
If you forget the password, run the local reset command from an interactive
terminal. With Docker Compose, run this while the container is running:

```bash
docker compose exec mangarr mangarr-entrypoint python -m mangarr.reset_password
```

On Unraid, open the Mangarr container console and run
`mangarr-entrypoint python -m mangarr.reset_password`.

For a direct Python install, run `python -m mangarr.reset_password` as the
service user with the same working directory, Python environment and
`MANGARR_DATA_DIR` as the server. Enter and confirm your new password at the
hidden prompts. The reset keeps your username, signs out all browser sessions,
and leaves API keys available throughout. Mangarr applies the change immediately.
The sign-in screen includes recovery instructions, and the reset command shows
your username before prompting.

After five failed sign-ins from the same client
in a one-minute window, the next attempt in that window returns HTTP 429 with
a `Retry-After` header. A successful sign-in clears the client's count. The
limit is per server process and groups IPv6 clients by their /64 prefix.
Clients behind a reverse proxy share its limit by default. To use the original
client addresses, set `FORWARDED_ALLOW_IPS` to the addresses of trusted proxies
that sanitize forwarding headers. If you launch Uvicorn directly, configure
`--forwarded-allow-ips` explicitly (`''` to trust no proxies).

When both `MANGARR_AUTH_USERNAME` and `MANGARR_AUTH_PASSWORD` are set, use
those credentials on the Mangarr sign-in page. They take precedence over an
app-managed login. Update the deployment credentials and restart Mangarr to
reset this login and sign out existing sessions. Setting only one fails startup.
Scripts can use the `/api/v1` endpoints with `X-Api-Key`.
Use HTTPS on a trusted reverse proxy when credentials travel across the network.

Custom deployments can explicitly set `MANGARR_ALLOW_UNAUTHENTICATED_BROWSER=true`
before creating a login and with both credential variables unset, for example when an authenticated reverse proxy
is the only route to Mangarr. In this mode any client with direct access to
Mangarr can obtain full API access. If an existing instance was accessible to
untrusted clients before upgrading, rotate its bootstrap and named API keys.

Host validation is always enabled, including for API clients. IP addresses
(IPv4 or bracketed IPv6) and `localhost` work by default. **Before upgrading**,
add every DNS name used by browsers, reverse proxies and integration clients to
`MANGARR_ALLOWED_HOSTS`, for example `'["mangarr", "manga.example.com"]'`.
Names match exactly, ignoring case, without a trailing dot; paths, schemes,
ports and wildcards are not allowed in the list. An unknown Host gets HTTP 400
with configuration instructions; direct IP access remains available for recovery.
Reverse proxies must preserve the browser's Host header for browser traffic.
For API-key clients, proxies can preserve an allowed Host or set it explicitly.
Forwarded headers never establish trust. Host validation limits DNS rebinding.

Existing named API keys and `X-Api-Key` clients continue to work independently
of browser sessions. When Mangarr manages the login, the browser uses a
session cookie for UI and API requests. API keys alone do not unlock browser
bootstrap when login is enabled. `/ping` remains public and contains no secrets.

Saved integration secrets are bound to the exact saved URL (including scheme,
port and path), and qBittorrent passwords also to the username. After changing
one of those fields, re-enter the secret before **Test** or **Save**. A masked
or omitted secret cannot be carried to a new destination; even cosmetic URL
edits require re-entry. Explicitly clearing a secret on Save removes it rather
than reusing it. Kavita's saved-library lookup can still omit the key at the
unchanged saved URL. HTTP client request/debug logs are suppressed even at
`DEBUG`, and connection errors omit remote URLs and response bodies to avoid
exposing integration credentials. ComicInfo metadata larger than 1 MiB is
ignored during matching/scanning, as is metadata compressed with methods other
than ZIP STORED or DEFLATE. The archive's normal filename/page matching continues.

### Source proxies

The shared **Content proxy URL** and source **Proxy** toggles route only chapter
image bytes. Search, metadata, and page manifests stay direct by default;
upgrading preserves these settings and routes.

Under **Settings → Source request proxies**, MangaFire and WeebCentral each
have an independent HTTP(S) proxy URL and routing selector. Choose **Proxy all
source requests and images** to explicitly route that source's search, chapter
metadata, page manifests, and images through its URL. MangaFire's frontend,
JavaScript retrieval, and token generation use that same route. This mode
overrides the shared content proxy and toggle for that source. Other sources
retain their existing content-only proxy support.

Proxy routes fail closed: missing or unreachable proxies produce an error,
without retrying directly. Disabled sources keep their routing requirements;
re-enabling a source requires its selected proxy URL. Saved proxy URLs are
masked because they may contain credentials. To replace a saved URL, enter
the complete address; to remove it, clear the field and select the default
routing mode (also clear the content toggle if direct images are wanted).
Settings changes apply to new requests/jobs. An active download keeps the
route it started with, including its page manifest and images.

WeebCentral sends browser-compatible HTML request headers. Neither headers
nor a proxy guarantee access: HTTP 403 can still depend on site policy and
network conditions. Mangarr does not solve challenges or bypass access controls.

### Chapter image optimization

In **Settings → Chapter Image Optimization**, opt in to reducing new direct-download
chapter sizes before CBZ packaging. It is **off by default**: original image bytes
are preserved. Existing library files and torrent imports are never recompressed.
Each active download snapshots the saved policy when the worker starts; changes
apply to subsequent downloads.

The live sample preview compares the original image with output from the same
encoder used by downloads. Drag the quality slider or change format/width to see
updated image bytes, dimensions and file-size savings without saving settings or
starting a download. Use **View at actual size (100%)** and scroll each image to
inspect fine text and artwork. The built-in sample is generated locally and
contains text, line art, halftones and color gradients; it never fetches external
images or modifies library files. With optimization off, both views show the
original image. Savings on the sample do not predict every chapter's savings.

- Choose lossy **WebP** (default) or **JPEG**, with quality **1–100** (default **85**).
  Even quality 100 is lossy. Savings depend on the source; inspect fine text, line
  art and gradients before choosing a lower quality. Some readers support JPEG
  more widely than WebP. Pages may retain mixed formats within a CBZ.
- **Maximum width** is **0** (original dimensions) by default, or **1–10000** pixels.
  EXIF orientation is applied first; resizing preserves aspect ratio, rounds the
  height to the nearest pixel, and never enlarges a page.
- If encoding is larger or equal in size, keep the original. When a supported
  page exceeds the explicit width limit, the resized output takes precedence
  over this size fallback, even if it is larger.
- Static JPEG, PNG and WebP can be optimized using Pillow. Animation (including
  APNG/WebP), GIF, AVIF, unsupported color modes, decoder/encoder failures, and
  pages above **32 MiB compressed or 20 megapixels** keep their original bytes.
  JPEG preserves transparent pages by keeping the original; WebP keeps alpha.
  ICC profiles are retained; rewritten pages discard other embedded metadata.
- Very tall pages that cannot fit the selected encoder (WebP: 16383 pixels per
  side; JPEG: 65500) stay original. Safety and format preservation take priority
  over the width limit. Skipped transformations are logged; pages are never
  silently dropped or split.

The packaged image extensions follow their actual bytes; page order and
ComicInfo metadata are retained. Archives whose page bytes changed include a small
`MangarrPageProvenance.json` member binding ordered original page CRC/size pairs
to the archived page identities. This keeps source-alias duplicate detection
working across format, quality, resize and enabled/disabled policy changes,
without treating identical lossy output from different originals as duplicates.
Volume merging carries these original identities forward.
Older archives continue using their actual page identities. Modified or invalid
provenance is ignored for duplicate decisions.

Transforms run off the async event loop, with one decoded page at a time and a
pixel/input-size limit. Cancellation waits for the current bounded transform to
finish, then prevents archive publication. The existing atomic CBZ publication
and partial-file cleanup remain in use. The downloader still holds the chapter's
compressed output page bytes in memory; optimization does not impose a new total
chapter size limit. WebP/JPEG encoder availability is checked when enabling the feature;
standard Pillow wheels in the Docker image supply both codecs. Custom Pillow
builds must provide the selected encoder.

## How grabbing works

1. When you add a series, mangarr links it to each enabled source by title
   (including MangaUpdates associated titles). Links are per-source, so a
   site changing its layout breaks one source, never the app.
2. The monitor job (default: every 60 min) diffs source chapter lists against
   the library. New monitored, missing chapters are grabbed from the highest
   priority source that has them (`Settings → Sources → priority`).
3. Direct grabs download pages with per-source rate limits and pack them into
   a CBZ. Add-time torrent searches inspect the small `.torrent` metadata
   first and rank releases by exact file coverage, special-chapter coverage,
   seeders, and size. The selected release goes to qBittorrent under the
   `mangarr` category and is imported when complete.

Please be a good citizen: respect source access controls, don't lower the rate
limits, and use a MangaDex account so their team can see the traffic is
legitimate.

## Roadmap

- Western comics support (ComicVine metadata + GetComics source) — the
  source/metadata plugin interfaces are already in place.
- Discord notifications, and notifications on grab (a webhook on import
  already exists — see Features).

## Torznab, Prowlarr, and Jackett (optional)

In **Settings → Torznab indexers**, add one or more indexers with a name,
Torznab API URL, API key, and comma-separated category IDs. Copy the API URL
from Prowlarr or Jackett (for example `http://prowlarr:9696/1/api` or
`http://jackett:9117/api/v2.0/indexers/my-indexer/results/torznab/api`). Use the
base API URL without query parameters; Mangarr supplies the key and query.
An aggregate Torznab endpoint can also be used when supported by your server.
The default category is comics (`7030`); choose the category IDs your indexer
uses for manga/books. Search type defaults to `search`; select `book` only
when the indexer supports it. API keys are masked after saving. Changing a
saved API URL requires re-entering its key.

Enable and configure qBittorrent to include torrent releases in **Search
Releases**. Nyaa and direct sources keep their existing controls. Results show
the configured feed/tracker, size, seeders, and leechers. Both magnets and
HTTP(S) torrent-file links work, including redirects to magnets. Torrent files
need a v1 btih identity: v1 and hybrid torrents are supported; v2-only metadata
is rejected before submission to qBittorrent. Completed
torrents use the existing chapter/volume importer, preserving the actual
source in Activity, history, and file provenance. Mount qBittorrent's completed
paths into Mangarr and configure the shared downloads directory. Hardlinks
keep seeding without duplicating data when downloads and library share a
filesystem; copy mode handles separate filesystems.

**Default torrent query templates** in Settings accept one variant per line
(up to 10 templates and 20 rendered queries per search). The default,
`{series}`, searches the title and known alternative titles. In a series'
**Automation → Torrent search options**, override the title or templates,
for example:

```text
{series} c{chapter:03d}
{series} v{volume:02d}
{series} {year}
```

Tokens are `{series}`, `{chapter}`, `{volume}`, and `{year}` (the series year).
Chapter padding preserves decimals: chapter 12.5 becomes `c012.5`. Volume
queries need a chapter-to-volume map. Chapter-scoped searches render those
chapters; series-wide searches render the known, non-excluded chapters.
If a numeric template has no applicable metadata, a title search is used.
Blank per-series fields restore the defaults. The release dialog also accepts
a manual torrent query; clear it to return to automatic variants. These query
settings do not change direct-source discovery.

Add-time **Search now** uses enabled indexers and Mangarr's existing coverage
selector: matching series title, maximum size, minimum seeders, then chapter
and volume coverage from torrent metadata (or a parseable release title when
metadata is unavailable). Volume-only releases need a chapter-to-volume map;
unknown coverage is never assumed to mean the entire series.

**Scheduled torrent fallback** is off by default. Enable it in Settings to
also search enabled torrent indexers after direct sources during scheduled
monitoring and monitored Search Missing passes. It respects monitored and
excluded chapters, requires at least one seeder, skips series with active
downloads or unavailable library roots, and applies the normal failure
cooldown to failed torrent releases. Explicit manual grabs remain available.
