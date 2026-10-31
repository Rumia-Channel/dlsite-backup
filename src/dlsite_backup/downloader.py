"""Download the files of a purchased work."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from dlsite_async import (
    EbookSession,
    EpubFixedSession,
    EpubReflowableSession,
    PlayAPI,
)
from dlsite_async.play.models import PlayFile

from .store import WorkStore, safe_relative_path


@dataclass
class WorkResult:
    """What happened to one work."""

    product_id: str
    title: str = ""
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_state(self) -> dict[str, Any]:
        """Return the state to write next to the files."""
        return {
            "product_id": self.product_id,
            "title": self.title,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
            "missing": self.missing,
        }


async def backup_purchases(
    play: PlayAPI,
    store: WorkStore,
    only: set[str] | None = None,
    force: bool = False,
    descramble: bool = True,
    limit: int | None = None,
    on_work: Any = None,
) -> list[WorkResult]:
    """Back up every purchased work (or the ones named in ``only``)."""
    results: list[WorkResult] = []
    async for work, purchase_date in play.purchases():
        if only and work.product_id not in only:
            continue
        if limit is not None and len(results) >= limit:
            break
        if on_work is not None:
            on_work(work, purchase_date)
        results.append(
            await backup_work(
                play,
                store,
                work,
                purchase_date=purchase_date,
                force=force,
                descramble=descramble,
            )
        )
    return results


async def backup_work(
    play: PlayAPI,
    store: WorkStore,
    work: Any,
    purchase_date: Any = None,
    force: bool = False,
    descramble: bool = True,
) -> WorkResult:
    """Back up a single work."""
    product_id = work.product_id
    result = WorkResult(product_id=product_id, title=work.work_name or "")
    store.write_metadata(product_id, work, purchase_date)

    try:
        token = await play.download_token(product_id)
        tree = await play.ziptree(token)
    except Exception as exc:  # noqa: BLE001 - one work must not stop the rest
        result.failed += 1
        result.errors.append(f"could not list the files: {exc}")
        store.write_state(product_id, result.to_state())
        return result

    for filename, playfile in tree.items():
        try:
            await _backup_playfile(
                play,
                store,
                token,
                tree,
                product_id,
                filename,
                playfile,
                result,
                force,
                descramble,
            )
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.errors.append(f"{filename}: {exc}")

    store.write_state(product_id, result.to_state())
    return result


async def _backup_playfile(
    play: PlayAPI,
    store: WorkStore,
    token: Any,
    tree: Any,
    product_id: str,
    filename: str,
    playfile: PlayFile,
    result: WorkResult,
    force: bool,
    descramble: bool,
) -> None:
    """Store one entry of the ziptree."""
    if playfile.type == "image":
        dest = store.playfile_path(product_id, _image_name(filename, playfile))
        await _download(play, token, playfile, dest, result, force, descramble)
        return

    if playfile.type == "pdf":
        # A PDF entry cannot be downloaded as a file; only its pages can.
        stem = safe_relative_path(filename).with_suffix("")
        for index, page in enumerate(playfile.files.get("page", [])):
            page_playfile = PlayFile(playfile.length, "image", page, "")
            try:
                extension = PurePosixPath(page_playfile.optimized_name).suffix
            except Exception:  # noqa: BLE001 - no optimized variant
                result.skipped += 1
                result.errors.append(f"{filename}[{index}]: no downloadable page")
                continue
            dest = store.playfile_path(
                product_id, f"{stem.as_posix()}/{index:03}{extension}"
            )
            await _download(play, token, page_playfile, dest, result, force, descramble)
        return

    if playfile.is_epub_reflowable:
        await _backup_reflowable_epub(
            play, store, tree, product_id, playfile, result, force
        )
        return

    if playfile.is_epub_fixed:
        await _backup_fixed_epub(
            play, store, tree, product_id, filename, playfile, result, force, descramble
        )
        return

    if playfile.is_ebook:
        await _backup_ebook(
            play, store, tree, product_id, filename, playfile, result, force
        )
        return

    if "optimized" in playfile.files:
        dest = store.playfile_path(product_id, _image_name(filename, playfile))
        await _download(play, token, playfile, dest, result, force, descramble)
        return

    result.skipped += 1
    result.errors.append(f"{filename}: no downloadable variant ({playfile.type})")


async def _download(
    play: PlayAPI,
    token: Any,
    playfile: PlayFile,
    dest: Path,
    result: WorkResult,
    force: bool,
    descramble: bool,
) -> None:
    """Download one file, skipping it when it is already there."""
    if "optimized" not in playfile.files:
        result.skipped += 1
        result.errors.append(f"{dest.name}: no web-optimized variant available")
        return
    if dest.exists() and not force:
        result.skipped += 1
        return
    try:
        await play.download_playfile(
            token, playfile, dest, mkdir=True, force=force, descramble=descramble
        )
        result.downloaded += 1
    except FileExistsError:
        result.skipped += 1
    except Exception as exc:  # noqa: BLE001
        result.failed += 1
        result.errors.append(f"{dest.name}: {exc}")


async def _backup_fixed_epub(
    play: PlayAPI,
    store: WorkStore,
    tree: Any,
    product_id: str,
    filename: str,
    playfile: PlayFile,
    result: WorkResult,
    force: bool,
    descramble: bool,
) -> None:
    """Store the pages of a fixed layout EPUB."""
    dest_dir = store.viewer_dir(product_id, safe_relative_path(filename).with_suffix("").as_posix())
    async with EpubFixedSession(play, tree, playfile, workno=product_id) as session:
        for index in range(session.page_count):
            try:
                await session.download_page(
                    index, dest_dir, mkdir=True, force=force, descramble=descramble
                )
                result.downloaded += 1
            except FileExistsError:
                result.skipped += 1
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                result.errors.append(f"{filename} page {index}: {exc}")


async def _backup_ebook(
    play: PlayAPI,
    store: WorkStore,
    tree: Any,
    product_id: str,
    filename: str,
    playfile: PlayFile,
    result: WorkResult,
    force: bool,
) -> None:
    """Store the pages of an ebook (comic, voice comic, webtoon)."""
    dest_dir = store.viewer_dir(product_id, safe_relative_path(filename).with_suffix("").as_posix())
    async with EbookSession(play, tree, playfile, workno=product_id) as session:
        for index in range(session.page_count):
            try:
                await session.download_page(
                    index, dest_dir, mkdir=True, force=force, convert="jpg"
                )
                result.downloaded += 1
            except FileExistsError:
                result.skipped += 1
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                result.errors.append(f"{filename} page {index}: {exc}")


async def _backup_reflowable_epub(
    play: PlayAPI,
    store: WorkStore,
    tree: Any,
    product_id: str,
    playfile: PlayFile,
    result: WorkResult,
    force: bool,
) -> None:
    """Store a reconstructed reflowable EPUB."""
    dest_dir = store.epub_dir(product_id)
    archive = dest_dir / f"{product_id}.epub"
    if archive.exists() and not force:
        result.skipped += 1
        return
    async with EpubReflowableSession(play, tree, playfile, workno=product_id) as session:
        try:
            await session.download_epub(dest_dir, mkdir=True, force=force)
        except FileExistsError:
            result.skipped += 1
            return
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.errors.append(f"{product_id}.epub: {exc}")
            return
    result.downloaded += 1
    # The viewer keeps going when a single manifest resource cannot be
    # fetched, so the archive is checked against its own manifest.
    result.missing.extend(verify_epub(archive))


def verify_epub(path: Path) -> list[str]:
    """Return the manifest resources that are missing from an EPUB.

    The viewer logs failed downloads instead of raising, so an archive can be
    written while parts of it are absent.
    """
    missing: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "META-INF/container.xml" not in names:
                return ["META-INF/container.xml"]
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            opf_path = None
            for element in container.iter():
                if element.tag.rsplit("}", 1)[-1] == "rootfile":
                    opf_path = element.get("full-path")
                    break
            if not opf_path:
                return ["(no rootfile)"]
            if opf_path not in names:
                return [opf_path]
            opf = ET.fromstring(archive.read(opf_path))
            base = PurePosixPath(opf_path).parent
            for element in opf.iter():
                if element.tag.rsplit("}", 1)[-1] != "item":
                    continue
                href = element.get("href")
                if not href:
                    continue
                target = PurePosixPath(href)
                if base != PurePosixPath("."):
                    target = base / target
                normalised = PurePosixPath(
                    *[part for part in target.parts if part != "."]
                ).as_posix()
                if normalised not in names:
                    missing.append(normalised)
    except (OSError, zipfile.BadZipFile, ET.ParseError) as exc:
        return [f"(unreadable archive: {exc})"]
    return missing


def _image_name(filename: str, playfile: PlayFile) -> str:
    """Return the name a ziptree entry is stored under.

    The name of the downloaded file is the one of the web-optimized variant,
    while the path of the original entry keeps the archive structure.
    """
    stem = safe_relative_path(filename).with_suffix("")
    try:
        extension = PurePosixPath(playfile.optimized_name).suffix
    except Exception:  # noqa: BLE001 - no optimized variant
        extension = ""
    return f"{stem.as_posix()}{extension}"
