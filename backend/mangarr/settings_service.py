"""Runtime-editable settings stored in the Settings table."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting

DEFAULTS: dict[str, str] = {
    # Media management
    "naming_template": "{series} - Ch. {chapter:04.1f}",
    "naming_template_no_volume": "{series} - Ch. {chapter:04.1f}",
    # Source priority: comma-separated source names, first = preferred.
    # Fast scanlation sources (tcbscans) ahead of archive sources so new
    # chapters are grabbed as soon as they appear.
    "source_priority": "mangaplus,tcbscans,mangadex,weebcentral,asura,viz,wikipedia,nyaa",
    # MangaDex credentials (personal API client)
    "mangadex_client_id": "",
    "mangadex_client_secret": "",
    "mangadex_username": "",
    "mangadex_password": "",
    "mangadex_language": "en",
    # qBittorrent
    "qbittorrent_url": "http://localhost:8080",
    "qbittorrent_username": "admin",
    "qbittorrent_password": "",
    "qbittorrent_category": "mangarr",
    "qbittorrent_enabled": "false",
    # Where torrents are downloaded to (a path both mangarr and qBittorrent
    # see, ideally on the same filesystem as the library so imports can
    # hardlink). Empty = qBittorrent's default save path.
    "downloads_dir": "",
    # Torrent import: "hardlink" keeps seeding without using double the
    # space (needs downloads + library on one filesystem); "copy" is the
    # safe cross-filesystem fallback.
    "import_mode": "hardlink",
    # Sources on/off
    "source_mangadex_enabled": "true",
    "source_weebcentral_enabled": "true",
    "source_tcbscans_enabled": "true",
    "source_asura_enabled": "true",
    # MangaPlus needs a residential IP (bans datacenters); off until the user
    # confirms it reaches the API from their host
    "source_mangaplus_enabled": "false",
    "source_nyaa_enabled": "false",
    # Wikipedia serves no chapters — it only contributes chapter→volume maps
    "source_wikipedia_enabled": "true",
    # Official metadata-only source for VIZ-licensed properties.
    "source_viz_enabled": "true",
    # Jobs
    "monitor_interval_minutes": "60",
    # Library
    "library_scan_on_add": "true",  # adopt existing on-disk files on add/refresh
}

SECRET_KEYS = {"mangadex_client_secret", "mangadex_password", "qbittorrent_password"}


def validate(values: dict[str, str]) -> None:
    """Reject values that would break things later if stored: a bad naming
    template fails every download at rename time, and a non-numeric monitor
    interval would abort scheduler startup. Raises ValueError."""
    from .library.naming import chapter_filename

    for key in ("naming_template", "naming_template_no_volume"):
        if key not in values:
            continue
        template = values[key]
        try:
            # render with and without a volume — both paths must work
            chapter_filename(template, template, "Sample Series", 12.5, 3, "Title")
            chapter_filename(template, template, "Sample Series", 12.5, None, "Title")
        except (KeyError, ValueError, IndexError) as exc:
            raise ValueError(
                f"{key} is not a valid template (use {{series}}, {{volume}}, "
                f"{{chapter}}, {{title}}): {exc}"
            ) from exc
    if "monitor_interval_minutes" in values:
        raw = values["monitor_interval_minutes"]
        try:
            minutes = int(raw)
        except (TypeError, ValueError):
            raise ValueError("monitor_interval_minutes must be a whole number") from None
        if minutes < 1:
            raise ValueError("monitor_interval_minutes must be at least 1")


async def get_all(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(Setting))).scalars().all()
    values = dict(DEFAULTS)
    values.update({r.key: r.value for r in rows if r.key in DEFAULTS})
    return values


async def get(session: AsyncSession, key: str) -> str:
    row = await session.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, "")


async def set_many(session: AsyncSession, values: dict[str, str]) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    await session.commit()
