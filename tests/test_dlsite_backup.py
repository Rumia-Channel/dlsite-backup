"""Tests for the parts of dlsite-backup that do not need a network."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from dlsite_backup.cookies import (
    SessionError,
    cookie_dict,
    decrypt,
    encrypt,
    filter_site,
    from_cdp,
    from_cookiejar,
    load_or_create_key,
    load_session,
    save_session,
    to_cookiejar,
)
from dlsite_backup.downloader import _image_name, verify_epub
from dlsite_backup.session import merge_cookies
from dlsite_backup.store import UnsafePathError, safe_relative_path


class FakePlayFile:
    """The bits of ``PlayFile`` that the naming rule looks at."""

    def __init__(self, optimized_name: str) -> None:
        self.optimized_name = optimized_name


# --- path safety ----------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    ["../escape", "a/../../escape", "/absolute/path", ".."],
)
def test_safe_relative_path_rejects_escapes(relative: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_relative_path(relative)


def test_safe_relative_path_keeps_normal_paths() -> None:
    assert safe_relative_path("package/001.jpg").as_posix() == "package/001.jpg"
    assert safe_relative_path("./package/001.jpg").as_posix() == "package/001.jpg"


# --- cookie storage -------------------------------------------------------


def test_encrypt_round_trip() -> None:
    key = b"k" * 32
    token = encrypt(key, b"secret")
    assert token.startswith("enc:v1:")
    assert "secret" not in token
    assert decrypt(key, token) == b"secret"


def test_decrypt_rejects_a_wrong_key() -> None:
    token = encrypt(b"k" * 32, b"secret")
    with pytest.raises(SessionError):
        decrypt(b"j" * 32, token)


def test_key_is_created_once(tmp_path: Path) -> None:
    path = tmp_path / "login.key"
    first = load_or_create_key(path)
    second = load_or_create_key(path)
    assert first == second
    assert len(first) == 32


def test_session_round_trip(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "login.key")
    cookies = [cookie_dict("dl_session", "value", ".dlsite.com")]
    path = tmp_path / "session.json"
    save_session(path, key, cookies)
    assert "value" not in path.read_text(encoding="utf-8")
    assert load_session(path, key) == cookies


def test_session_without_a_key_file_is_reported(tmp_path: Path) -> None:
    key = b"k" * 32
    with pytest.raises(SessionError):
        load_session(tmp_path / "missing.json", key)


# --- cookie domains -------------------------------------------------------


def test_filter_site_keeps_the_domain_family() -> None:
    cookies = [
        cookie_dict("a", "1", ".dlsite.com"),
        cookie_dict("b", "2", "login.dlsite.com"),
        cookie_dict("c", "3", "play.dl.dlsite.com"),
        cookie_dict("d", "4", ".example.com"),
        cookie_dict("e", "5", "notdlsite.com"),
    ]
    kept = {c["name"] for c in filter_site(cookies)}
    assert kept == {"a", "b", "c"}


def test_cookiejar_keeps_the_domain_scope() -> None:
    jar = to_cookiejar(
        [
            cookie_dict("domain_wide", "1", ".dlsite.com"),
            cookie_dict("host_only", "2", "login.dlsite.com"),
        ]
    )
    scoped = {cookie.name: cookie for cookie in jar}
    assert scoped["domain_wide"].domain == ".dlsite.com"
    assert scoped["domain_wide"].domain_specified is True
    assert scoped["host_only"].domain == "login.dlsite.com"
    assert scoped["host_only"].domain_specified is False


def test_cookiejar_round_trip() -> None:
    cookies = [
        cookie_dict("a", "1", ".dlsite.com", path="/x", secure=True, http_only=True),
        cookie_dict("b", "2", "play.dlsite.com"),
    ]
    restored = from_cookiejar(to_cookiejar(cookies))
    assert {c["name"] for c in restored} == {"a", "b"}
    a = next(c for c in restored if c["name"] == "a")
    assert a["domain"] == ".dlsite.com"
    assert a["secure"] is True
    assert a["httpOnly"] is True
    assert a["path"] == "/x"


def test_cdp_cookies_are_converted() -> None:
    records = from_cdp(
        [
            {
                "name": "dl_session",
                "value": "abc",
                "domain": ".dlsite.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": -1,
            }
        ]
    )
    assert records[0]["name"] == "dl_session"
    assert records[0]["expires"] is None


def test_merge_cookies_lets_the_later_set_win() -> None:
    merged = merge_cookies(
        [cookie_dict("a", "old", ".dlsite.com")],
        [cookie_dict("a", "new", ".dlsite.com")],
    )
    assert [c["value"] for c in merged] == ["new"]


# --- naming ----------------------------------------------------------------


def test_image_name_uses_the_optimized_extension() -> None:
    assert (
        _image_name("package/001.png", FakePlayFile("package/001__opt.jpg"))
        == "package/001.jpg"
    )


def test_image_name_keeps_the_original_path() -> None:
    assert (
        _image_name("deep/dir/page.webp", FakePlayFile("page__opt.png"))
        == "deep/dir/page.png"
    )


# --- epub verification -----------------------------------------------------


def _write_epub(path: Path, *, omit: str | None = None) -> None:
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
    entries = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": container,
        "OEBPS/content.opf": opf,
        "OEBPS/nav.xhtml": "<html/>",
        "OEBPS/images/001.jpg": "binary",
    }
    if omit:
        entries.pop(omit)
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def test_verify_epub_accepts_a_complete_archive(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    _write_epub(path)
    assert verify_epub(path) == []


def test_verify_epub_reports_a_missing_resource(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    _write_epub(path, omit="OEBPS/images/001.jpg")
    assert verify_epub(path) == ["OEBPS/images/001.jpg"]


def test_verify_epub_reports_a_broken_archive(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    path.write_bytes(b"not a zip")
    assert verify_epub(path) != []
