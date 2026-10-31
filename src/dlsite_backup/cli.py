"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError

from . import config
from .browser import BrowserNotFoundError, collect_login_cookies, find_browser
from .cookies import (
    SessionError,
    filter_site,
    from_cdp,
    in_site_family,
    load_or_create_key,
    load_session,
    save_session,
)
from .downloader import backup_purchases
from .session import (
    build_play_api,
    establish_play_session,
    merge_cookies,
    session_works,
)
from .store import WorkStore


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dlsite-backup",
        description="Back up DLsite Play purchases to local storage.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="directory holding the session cookies and the browser profile "
        f"(default: {config.default_home()})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser(
        "login", help="log in through your browser and store the session"
    )
    login.add_argument(
        "--browser", default=None, help="path of the browser to use"
    )
    login.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="browser profile to use (default: a dedicated one that keeps "
        "the browser's clearance cookie)",
    )
    login.add_argument(
        "--timeout", type=float, default=300.0, help="seconds to wait for the login"
    )

    listing = sub.add_parser("list", help="list the purchased works")
    listing.add_argument("--limit", type=int, default=None)

    backup = sub.add_parser("backup", help="download the purchased works")
    backup.add_argument(
        "--dest",
        type=Path,
        default=Path("dlsite-backup"),
        help="directory to write the backup into (default: ./dlsite-backup)",
    )
    backup.add_argument(
        "--work",
        action="append",
        default=None,
        metavar="PRODUCT_ID",
        help="only back up this product id (may be given more than once)",
    )
    backup.add_argument(
        "--limit", type=int, default=None, help="stop after this many works"
    )
    backup.add_argument(
        "--force", action="store_true", help="download again even if the file is there"
    )
    backup.add_argument(
        "--no-descramble",
        action="store_true",
        help="keep the images scrambled instead of restoring them",
    )

    status = sub.add_parser("status", help="show what has been backed up")
    status.add_argument(
        "--dest",
        type=Path,
        default=Path("dlsite-backup"),
        help="the backup directory to inspect",
    )
    return parser


def _home(args: argparse.Namespace) -> Path:
    return Path(args.home) if args.home else config.default_home()


def _stored_cookies(args: argparse.Namespace) -> list[dict]:
    home = _home(args)
    key = load_or_create_key(config.key_path(home))
    return load_session(config.session_path(home), key)


async def _login(args: argparse.Namespace) -> int:
    home = _home(args)
    key = load_or_create_key(config.key_path(home))

    # A session that still works does not need the browser.
    try:
        existing = load_session(config.session_path(home), key)
    except SessionError:
        existing = []
    if existing and await session_works(existing):
        print("保存済みのセッションがまだ有効です。ログインの必要はありません。")
        return 0

    browser = find_browser(args.browser)
    profile = args.profile or config.profile_path(home)
    print(f"ブラウザ: {browser}")

    async def authenticated(raw_cookies: list[dict]) -> bool:
        """Return whether the browser already holds a working session."""
        return await session_works(filter_site(from_cdp(raw_cookies)))

    def site_filter(cookie: dict) -> bool:
        """Return whether a browser cookie belongs to the DLsite domain family."""
        return in_site_family(str(cookie.get("domain", "")))

    raw = await collect_login_cookies(
        browser,
        profile,
        timeout=args.timeout,
        authenticated=authenticated,
        site_filter=site_filter,
    )
    cookies = filter_site(from_cdp(raw))
    if not cookies:
        print(
            "DLsite の Cookie が取得できませんでした。ログインが完了しているか確認してください。",
            file=sys.stderr,
        )
        return 1

    # The Play session is issued to requests that come from the Play site.
    cookies = merge_cookies(cookies, await establish_play_session(cookies))
    if not await session_works(cookies):
        print(
            "保存した Cookie では DLsite Play の API にアクセスできませんでした。\n"
            "ブラウザでログインが完了しているか確認し、もう一度 "
            "`dlsite-backup login` を実行してください。",
            file=sys.stderr,
        )
        return 1
    save_session(config.session_path(home), key, cookies)
    print(f"{len(cookies)} 件の Cookie を {config.session_path(home)} に保存しました。")
    return 0


async def _list(args: argparse.Namespace) -> int:
    cookies = _stored_cookies(args)
    async with build_play_api(cookies) as play:
        count = 0
        async for work, purchase_date in play.purchases():
            print(
                f"{work.product_id}\t{work.work_name}\t"
                f"{purchase_date.isoformat() if purchase_date else ''}"
            )
            count += 1
            if args.limit is not None and count >= args.limit:
                break
    return 0


async def _backup(args: argparse.Namespace) -> int:
    cookies = _stored_cookies(args)
    store = WorkStore(args.dest)
    only = set(args.work) if args.work else None

    def report(work: object, purchase_date: object) -> None:
        name = getattr(work, "work_name", "") or ""
        print(f"[{work.product_id}] {name}", flush=True)

    async with build_play_api(cookies) as play:
        results = await backup_purchases(
            play,
            store,
            only=only,
            force=args.force,
            descramble=not args.no_descramble,
            limit=args.limit,
            on_work=report,
        )

    downloaded = sum(r.downloaded for r in results)
    skipped = sum(r.skipped for r in results)
    failed = sum(r.failed for r in results)
    missing = sum(len(r.missing) for r in results)
    print(
        f"\n{len(results)} 作品: 保存 {downloaded} / 既存 {skipped} / "
        f"失敗 {failed} / 欠落 {missing}"
    )
    for result in results:
        for message in result.errors:
            print(f"  ! {result.product_id}: {message}", file=sys.stderr)
        for name in result.missing:
            print(f"  ! {result.product_id}: 欠落 {name}", file=sys.stderr)
    return 1 if failed else 0


def _status(args: argparse.Namespace) -> int:
    store = WorkStore(args.dest)
    product_ids = store.stored_work_ids()
    if not product_ids:
        print(f"{store.root} にはまだ何も保存されていません。")
        return 0
    for product_id in product_ids:
        state = store.read_state(product_id)
        print(
            f"{product_id}\t{state.get('title', '')}\t"
            f"保存 {state.get('downloaded', 0)} / 既存 {state.get('skipped', 0)} / "
            f"失敗 {state.get('failed', 0)}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "login":
            return asyncio.run(_login(args))
        if args.command == "list":
            return asyncio.run(_list(args))
        if args.command == "backup":
            return asyncio.run(_backup(args))
        if args.command == "status":
            return _status(args)
    except SessionError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except BrowserNotFoundError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except CurlHTTPError as exc:
        print(
            f"エラー: DLsite が要求を拒否しました ({exc}).\n"
            "セッションの有効期限が切れている可能性があります。"
            "`dlsite-backup login` を実行してください。",
            file=sys.stderr,
        )
        return 3
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        return 130
    parser.error(f"unknown command {args.command}")
    return 2
