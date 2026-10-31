"""Tests for the download orchestration, with a stand-in for the Play API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from dlsite_async.play.models import PlayFile

from dlsite_backup.downloader import backup_work
from dlsite_backup.store import WorkStore


@dataclass
class FakeWork:
    product_id: str
    work_name: str = "a work"


@dataclass
class FakePlayAPI:
    """Records what was asked of it and writes the files it is given."""

    entries: dict[str, PlayFile] = field(default_factory=dict)
    downloads: list[str] = field(default_factory=list)

    async def download_token(self, workno: str) -> str:
        return f"token-{workno}"

    async def ziptree(self, token: str) -> dict[str, PlayFile]:
        return self.entries

    async def download_playfile(
        self,
        token: Any,
        playfile: PlayFile,
        dest: str | Path,
        mkdir: bool = False,
        force: bool = False,
        descramble: bool = False,
        **save_kwargs: Any,
    ) -> None:
        dest = Path(dest)
        if dest.exists() and not force:
            raise FileExistsError(str(dest))
        if mkdir:
            dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"payload")
        self.downloads.append(dest.name)


def image_entry(name: str = "001__opt.jpg") -> PlayFile:
    """Return a playfile that looks like a web-optimized image."""
    return PlayFile(
        length=1024,
        type="image",
        files={
            "optimized": {"name": name, "width": 800, "height": 1200},
            "original": {"name": "001.png"},
        },
        hashname="h",
    )


@pytest.mark.asyncio
async def test_image_is_stored_under_its_own_path(tmp_path: Path) -> None:
    play = FakePlayAPI(entries={"package/page.png": image_entry()})
    store = WorkStore(tmp_path)

    result = await backup_work(play, store, FakeWork("BJ000001"))

    assert result.downloaded == 1
    assert result.failed == 0
    stored = tmp_path / "works" / "BJ000001" / "playfiles" / "package" / "page.jpg"
    assert stored.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_an_existing_file_is_skipped(tmp_path: Path) -> None:
    play = FakePlayAPI(entries={"package/page.png": image_entry()})
    store = WorkStore(tmp_path)
    stored = tmp_path / "works" / "BJ000001" / "playfiles" / "package" / "page.jpg"
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"already here")

    result = await backup_work(play, store, FakeWork("BJ000001"))

    assert result.skipped == 1
    assert result.downloaded == 0
    assert stored.read_bytes() == b"already here"


@pytest.mark.asyncio
async def test_force_downloads_again(tmp_path: Path) -> None:
    play = FakePlayAPI(entries={"package/page.png": image_entry()})
    store = WorkStore(tmp_path)
    stored = tmp_path / "works" / "BJ000001" / "playfiles" / "package" / "page.jpg"
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"old")

    result = await backup_work(play, store, FakeWork("BJ000001"), force=True)

    assert result.downloaded == 1
    assert stored.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_metadata_and_state_are_written(tmp_path: Path) -> None:
    play = FakePlayAPI(entries={"package/page.png": image_entry()})
    store = WorkStore(tmp_path)

    await backup_work(play, store, FakeWork("BJ000001"), purchase_date="2026-01-02")

    metadata = json.loads(
        (tmp_path / "works" / "BJ000001" / "work.json").read_text(encoding="utf-8")
    )
    assert metadata["product_id"] == "BJ000001"
    assert metadata["purchase_date"] == "2026-01-02"
    assert metadata["work"]["work_name"] == "a work"

    state = json.loads(
        (tmp_path / "works" / "BJ000001" / "state.json").read_text(encoding="utf-8")
    )
    assert state["downloaded"] == 1
    assert state["failed"] == 0


@pytest.mark.asyncio
async def test_a_file_without_a_downloadable_variant_is_reported(tmp_path: Path) -> None:
    play = FakePlayAPI(
        entries={
            "movie.mpg": PlayFile(
                length=10, type="movie", files={"original": {"name": "movie.mpg"}}, hashname=""
            )
        }
    )
    store = WorkStore(tmp_path)

    result = await backup_work(play, store, FakeWork("BJ000001"))

    assert result.failed == 0
    assert result.skipped == 1
    assert any("no downloadable variant" in message for message in result.errors)


@pytest.mark.asyncio
async def test_a_failing_work_is_recorded_not_raised(tmp_path: Path) -> None:
    class BrokenPlayAPI(FakePlayAPI):
        async def download_token(self, workno: str) -> str:
            raise RuntimeError("no token for you")

    store = WorkStore(tmp_path)

    result = await backup_work(BrokenPlayAPI(), store, FakeWork("BJ000001"))

    assert result.failed == 1
    assert "no token for you" in result.errors[0]
    state = json.loads(
        (tmp_path / "works" / "BJ000001" / "state.json").read_text(encoding="utf-8")
    )
    assert state["failed"] == 1


@pytest.mark.asyncio
async def test_pdf_pages_are_stored_next_to_each_other(tmp_path: Path) -> None:
    pdf = PlayFile(
        length=2048,
        type="pdf",
        files={
            "page": [
                {"optimized": {"name": "001__opt.jpg", "width": 800, "height": 1200}},
                {"optimized": {"name": "002__opt.jpg", "width": 800, "height": 1200}},
            ]
        },
        hashname="h",
    )
    play = FakePlayAPI(entries={"book/book.pdf": pdf})
    store = WorkStore(tmp_path)

    result = await backup_work(play, store, FakeWork("BJ000001"))

    assert result.downloaded == 2
    base = tmp_path / "works" / "BJ000001" / "playfiles" / "book" / "book"
    assert (base / "000.jpg").is_file()
    assert (base / "001.jpg").is_file()
