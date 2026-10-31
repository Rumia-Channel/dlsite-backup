"""Tests for the EPUB branches of the download orchestration.

The viewer sessions themselves belong to ``dlsite-async``; what is checked
here is that a work is routed to the right session, that the result lands in
the right place and that a partially downloaded archive is reported.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from dlsite_async.play.models import PlayFile

from dlsite_backup import downloader
from dlsite_backup.downloader import backup_work
from dlsite_backup.store import WorkStore

from test_downloader import FakePlayAPI, FakeWork


class FakeFixedSession:
    """Stands in for ``EpubFixedSession``."""

    calls: list[tuple[Any, ...]] = []

    def __init__(self, play: Any, tree: Any, playfile: Any, workno: str | None = None):
        self.workno = workno
        self.page_count = 3
        FakeFixedSession.calls.append(("init", workno))

    async def __aenter__(self) -> "FakeFixedSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def download_page(
        self,
        index: int,
        dest_dir: str | Path,
        mkdir: bool = False,
        descramble: bool = False,
        force: bool = False,
        **save_kwargs: Any,
    ) -> list[Path]:
        dest = Path(dest_dir) / f"{index:04}.jpg"
        if mkdir:
            dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"page")
        FakeFixedSession.calls.append(("page", index, str(dest)))
        return [dest]


class FakeReflowableSession:
    """Stands in for ``EpubReflowableSession``."""

    calls: list[tuple[Any, ...]] = []
    archive_content: bytes = b""

    def __init__(self, play: Any, tree: Any, playfile: Any, workno: str | None = None):
        self.workno = workno
        FakeReflowableSession.calls.append(("init", workno))

    async def __aenter__(self) -> "FakeReflowableSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def download_epub(
        self,
        dest_dir: str | Path,
        mkdir: bool = False,
        force: bool = False,
        **save_kwargs: Any,
    ) -> Path:
        dest = Path(dest_dir) / f"{self.workno}.epub"
        if mkdir:
            dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.archive_content)
        FakeReflowableSession.calls.append(("epub", str(dest)))
        return dest


def make_epub(*, complete: bool) -> bytes:
    """Return the bytes of a small EPUB archive."""
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"/>
    <item id="img" href="images/001.jpg" media-type="image/jpeg"/>
  </manifest>
</package>
"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/nav.xhtml", "<html/>")
        if complete:
            archive.writestr("OEBPS/images/001.jpg", "binary")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    FakeFixedSession.calls = []
    FakeReflowableSession.calls = []


@pytest.mark.asyncio
async def test_fixed_epub_pages_go_to_the_viewer_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "EpubFixedSession", FakeFixedSession)
    play = FakePlayAPI(
        entries={
            "book/book.epub": PlayFile(
                length=100, type="epub", files={"optimized": {"name": "book.epub"}}, hashname="h"
            )
        }
    )
    store = WorkStore(tmp_path)

    result = await backup_work(play, store, FakeWork("BJ000001"))

    assert result.downloaded == 3
    assert result.failed == 0
    pages = tmp_path / "works" / "BJ000001" / "viewer" / "book" / "book"
    assert (pages / "0000.jpg").is_file()
    assert (pages / "0002.jpg").is_file()
    # The work id is handed to the session so that it does not depend on the
    # ziptree carrying one.
    assert ("init", "BJ000001") in FakeFixedSession.calls


@pytest.mark.asyncio
async def test_reflowable_epub_is_stored_and_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "EpubReflowableSession", FakeReflowableSession)
    FakeReflowableSession.archive_content = make_epub(complete=True)
    play = FakePlayAPI(
        entries={
            "book/book.epub": PlayFile(
                length=100,
                type="epub_reflowable",
                files={"optimized": {"name": "book.epub"}},
                hashname="h",
            )
        }
    )
    store = WorkStore(tmp_path)

    result = await backup_work(play, store, FakeWork("BJ000001"))

    assert result.downloaded == 1
    assert result.missing == []
    archive = tmp_path / "works" / "BJ000001" / "epub" / "BJ000001.epub"
    assert archive.is_file()


@pytest.mark.asyncio
async def test_a_partially_downloaded_epub_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "EpubReflowableSession", FakeReflowableSession)
    FakeReflowableSession.archive_content = make_epub(complete=False)
    play = FakePlayAPI(
        entries={
            "book/book.epub": PlayFile(
                length=100,
                type="epub_reflowable",
                files={"optimized": {"name": "book.epub"}},
                hashname="h",
            )
        }
    )
    store = WorkStore(tmp_path)

    result = await backup_work(play, store, FakeWork("BJ000001"))

    assert result.downloaded == 1
    assert result.missing == ["OEBPS/images/001.jpg"]
    state = (tmp_path / "works" / "BJ000001" / "state.json").read_text(encoding="utf-8")
    assert "OEBPS/images/001.jpg" in state


@pytest.mark.asyncio
async def test_an_existing_epub_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloader, "EpubReflowableSession", FakeReflowableSession)
    FakeReflowableSession.archive_content = make_epub(complete=True)
    play = FakePlayAPI(
        entries={
            "book/book.epub": PlayFile(
                length=100,
                type="epub_reflowable",
                files={"optimized": {"name": "book.epub"}},
                hashname="h",
            )
        }
    )
    store = WorkStore(tmp_path)
    archive = tmp_path / "works" / "BJ000001" / "epub" / "BJ000001.epub"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"already here")

    result = await backup_work(play, store, FakeWork("BJ000001"))

    assert result.skipped == 1
    assert result.downloaded == 0
    assert archive.read_bytes() == b"already here"
