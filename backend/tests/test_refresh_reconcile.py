import pytest

from mangarr.jobs.tasks import reconcile_downloaded_files
from mangarr.models import Chapter, Series


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_reconcile_clears_downloaded_when_file_missing(tmp_path):
    present = tmp_path / "present.cbz"
    present.write_bytes(b"cbz")
    missing = tmp_path / "missing.cbz"
    series = Series(id=1, title="Kagurabachi")
    chapters = [
        Chapter(id=1, series_id=1, number=1, downloaded=True, file_path=str(present)),
        Chapter(id=2, series_id=1, number=2, downloaded=True, file_path=str(missing)),
        Chapter(id=3, series_id=1, number=3, downloaded=False, file_path=""),
    ]
    series.chapters = chapters
    session = FakeSession()

    changed = await reconcile_downloaded_files(session, series)

    assert changed == 1
    assert session.commits == 1
    assert chapters[0].downloaded is True
    assert chapters[0].file_path == str(present)
    assert chapters[1].downloaded is False
    assert chapters[1].file_path == ""
    assert chapters[2].downloaded is False


@pytest.mark.asyncio
async def test_reconcile_does_not_commit_when_nothing_changed(tmp_path):
    present = tmp_path / "present.cbz"
    present.write_bytes(b"cbz")
    series = Series(id=1, title="Kagurabachi")
    series.chapters = [
        Chapter(id=1, series_id=1, number=1, downloaded=True, file_path=str(present)),
        Chapter(id=2, series_id=1, number=2, downloaded=False, file_path=""),
    ]
    session = FakeSession()

    changed = await reconcile_downloaded_files(session, series)

    assert changed == 0
    assert session.commits == 0
