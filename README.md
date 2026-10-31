# dlsite-backup

DLsite Play の購入済み作品をローカルにバックアップする CLI ツールです。

## インストール

```powershell
uv sync
uv run dlsite-backup --help
```

## 使い方

### 1. ログイン

```powershell
uv run dlsite-backup login
```

ブラウザ（既定は Google Chrome、見つからなければ Edge などの Chromium 系）が開き、
DLsite のログイン画面が表示されます。ログインを済ませたら、**この画面で Enter キーを
押してください**。Enter を押すまでブラウザは閉じません（ログインの完了は API を
使って自動検知し、「ログインを確認しました」と通知します）。

ブラウザを先に閉じた場合も、ログイン済みセッションはブラウザプロファイルに残って
いるため、そこから読み直します。

既に保存済みのセッションが有効なときは、ブラウザを起動せずに「まだ有効です」とだけ
知らせます。

Cookie は `~/.dlsite-backup/session.json` に**暗号化して**保存されます。暗号鍵は
同じディレクトリの `login.key` にあります。鍵と暗号文を同じ場所に置かないでください。

- `--browser <パス>` … 使用するブラウザを指定
- `--profile <DIR>` … ブラウザのプロファイルを指定（既定は専用プロファイル。
  Cloudflare のクリアランス Cookie が残ります）
- `--timeout <秒>` … ログインを待つ時間（既定 300）

### 2. 購入作品の一覧

```powershell
uv run dlsite-backup list
```

### 3. バックアップ

```powershell
uv run dlsite-backup backup --dest D:\dlsite
```

- `--dest <DIR>` … 保存先（既定 `./dlsite-backup`）
- `--work <PRODUCT_ID>` … 特定の作品だけ。複数指定可
- `--limit <N>` … N 作品で打ち切る
- `--force` … 保存済みでも取り直す
- `--no-descramble` … 復号せず画像が縞のままで保存

### 4. 保存状況

```powershell
uv run dlsite-backup status --dest D:\dlsite
```

## 保存されるもの

```
<dest>/
  works/
    <product_id>/
      work.json          作品のメタデータ（購入日を含む）
      state.json         取得結果とエラー
      playfiles/         ziptree そのままのファイル（画像、PDF のページ等）
      viewer/            ビューア経由のページ（コミック、Webtoon、固定-layout EPUB）
      epub/              再構築した可変-layout EPUB
```

保存済みのファイルは読み飛ばすので、途中で止めた実行を同じコマンドで再開できます。

### 取得できないもの

- **PDF ファイル本体** — DLsite Play は PDF の直接ダウンロードを認めていません。
  代わりに、その PDF のページ画像（`.jpg`）がページ単位で保存されます。
- **配信終了の作品** — 購入一覧には残っていても、ファイルが既に削除された作品は
  404 になります。作品ごとにスキップせず `state.json` の `errors` に記録し、
  処理は続行します（終了コードは 1）。
- **EPUB** — 固定レイアウト / 可変レイアウトの EPUB は、ビューア API 経由で
  再構築します（可変レイアウトは manifest と突き合わせて欠落を報告します）。

## 設計の要点

### ログインはブラウザに任せる

DLsite は Cloudflare のボット対策を強化していて、HTTP レベルのログインは 403 を
返します（2026-10 時点では aiohttp では常に失敗し、curl-cffi の TLS フィンガープリント
偽装が必要）。そこで本ツールは実ブラウザにログインさせ、DevTools プロトコル
（`Storage.getCookies`）で Cookie を取り出す方式に統一しました。二段階認証や
CAPTCHA は通常のブラウザと同じ挙動のまま通ります。

取り出した Cookie は `dlsite-async` のセッションに注入し、以降の API 呼び出しと
ダウンロードは HTTP クライアント経由で行います。Cloudflare の保護があるのは
ブラウザが描く画面だけで、API エンドポイントは保護されていない（未認証でも
正規の JSON が返る）ため、ブラウザはログインの 1 回だけです。

### 依存

- [`dlsite-async`](https://github.com/bhrevol/dlsite-async) — DLsite の API クライアント
  （`>=1.0.0b4`。1.0.0b1 で HTTP 層が aiohttp から curl-cffi に差し替わっており、
  0.11 系では Cloudflare に 403 されます）
- `websockets` — DevTools プロトコルのクライアント
- `cryptography` — Cookie の暗号化（ChaCha20-Poly1305）

ブラウザ自動化ライブラリ（Playwright 等）は使っていません。CDP を直接叩くだけなので、
追加のブラウザのダウンロードも要りません。

### 壊れやすい部分

- **Cloudflare の判定** — ブラウザが 403 になる場合は、プロファイルを作り直すと
  解消する場合があります（`--profile` で別のプロファイルを指す）
- **EPUB の再構築** — ビューアは取得に失敗したリソースを握り潰すため、復元した
  EPUB は manifest と突き合わせて `state.json` の `missing` に欠落として報告します
