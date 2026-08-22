# さいたまマラソン2027 ゆずれ～る枠 監視ツール

RUNNET の「ゆずれ～る」（出走権譲渡）で **さいたまマラソン2027 マラソンの部**（raceId=392524）に
空き枠が出たら、iPhoneプッシュ（ntfy）とメールで通知する。

**エントリー・決済は自動化しない。** 通知を受けて自分で手動エントリーする。
ログイン情報は保存も入力もしない。

---

## 0. 実績（2026-08-19）

**実際に枠が2回出て、2回とも検知・通知に成功している。**

| 検知 | 消滅 | 猶予 |
|---|---|---|
| 8/19 20:15 | 20:30 までに消滅 | 15分以内 |
| 8/19 22:30 | 22:48 までに消滅 | 18分以内 |

**枠は15〜18分で埋まる。** この実測に基づき日中の間隔を15分→5分に詰めた。
通知に気づいてから動ける時間は10分あるかないかなので、iPhoneプッシュ（下記）は事実上必須。

---

## 1. これは何を見ているか

RUNNET の「ゆずれ～る実施大会一覧」の各行にある空き状況バルーン画像を読む。

| 画像 | 状態 | 通知 |
|---|---|---|
| `balloon_yuzureru_yoko.png` | ゆずれ～る開始前 | しない |
| `balloon_yuzureru_none.png` | 期間中だが空きなし | しない |
| `balloon_yuzureru_sankaku.png` | **エントリー可・残りわずか** | **する** |
| 上記以外の balyuzureru 画像 | 未知（空きありの別画像の可能性） | **する** |

最後の行が重要で、RUNNET が「空きあり」用の別画像を持っていた場合でも取りこぼさないよう、
**既知の「空きなし・開始前」以外はすべて枠ありとみなす**（フェイルオープン）。
誤検知が1回増えるより、取りこぼしゼロを優先している。

### ⚠️ 大会詳細ページの緑ボタンは使っていない（重要）

詳細ページの緑ボタン `class="btEntryMainYuzureru"` は **空き枠とは無関係**。
2026-08-16 に対照実験で確認済み：

| raceId | 実際の枠 | 緑ボタン |
|---|---|---|
| 388460 | 枠あり | なし |
| 394736 / 386333 | 空きなし | なし |
| **392524（本大会）** | **空きなし** | **あり** |

これは「定員締切済みでゆずれ～るが唯一の入口」を示すマークで、
判定に使うと **初回実行から永久に誤検知する**。将来手を入れる際も使わないこと。

### 取得できないもの

種目選択画面はログイン必須（未ログインだとログイン画面へリダイレクトされる）。
そのため **正確な残枠数は取得できない**。通知は「残りわずか表示が出た」までを伝える。

---

## 2. セットアップA: ローカル launchd（現在の構成・稼働中 ✅）

**設定・登録済みで、すでに動いている。追加作業は不要。**

- 実行間隔：6:00〜23:55 は**5分ごと**、0:00〜5:45 は15分ごと（1日240回）
- 通知：メール。既存 `wonder-gym-mail` の Keychain からアプリパスワードを自動で読むため、
  新たな認証情報の登録は不要
- 宛先：plist の `MAIL_FROM` / `MAIL_TO` で指定（個人情報はリポジトリに置かず plist だけに持たせている）
- ログ：`~/Library/Logs/yuzureru-monitor.log`
- 定義ファイル：`~/Library/LaunchAgents/com.junsugata.yuzureru-monitor.plist`

### iPhoneプッシュ（ntfy）— 設定済み。購読するだけ

トピック名は plist に設定済み。実物は **`NTFY_TOPIC.txt`**（gitignore 済み）で確認する：

```bash
cat ~/yuzureru-monitor/NTFY_TOPIC.txt
```

1. App Store で **ntfy** をインストール
2. **Subscribe to topic** に上記コマンドで出た文字列を入力

これだけでプッシュが届くようになる。トピック名を知っていれば誰でも購読・投稿できるため、
この文字列は人に見せないこと。変えたくなったら plist の `NTFY_TOPIC` を書き換えて再読み込み：

```bash
launchctl unload ~/Library/LaunchAgents/com.junsugata.yuzureru-monitor.plist && launchctl load -w ~/Library/LaunchAgents/com.junsugata.yuzureru-monitor.plist
```

### ⚠️ この構成の弱点

**MacBook がスリープ中・電源オフの間は実行されない。** launchd は復帰時に取りこぼした分を
1回だけまとめて実行するが、その時点では枠が消えている可能性が高い。
夜間と外出時（火・木の早朝など）に1日8〜10時間の空白が生じる見込み。
24時間監視にしたくなったら次章へ。

---

## 3. セットアップB: GitHub Actions（24時間監視にしたい場合）

こちらは**未設定**。ローカル版と併用してもよい（両方が検知すると通知が重複するだけで、取りこぼしは増えない）。

### 2-1. リポジトリを作る

```bash
cd ~/yuzureru-monitor && git add -A && git commit -m "init: ゆずれ～る枠 監視ツール"
```

その後 GitHub にプッシュする。

> **リポジトリは Public 推奨。** Public なら Actions の実行時間が無料無制限。
> Private だと無料枠2,000分/月に対して本ツールは月1,300〜1,700分ほど使い、上限に近い。
> コードに秘密情報は一切含めていない（すべて Secrets 経由）ので Public で問題ない。

### 2-2. Secrets を登録する

リポジトリの Settings → Secrets and variables → Actions → New repository secret。

| 名前 | 中身 | 必須 |
|---|---|---|
| `NTFY_TOPIC` | ntfy のトピック名（下記 2-3） | 推奨 |
| `GMAIL_APP_PASSWORD` | Gmail アプリパスワード16桁 | 推奨 |
| `MAIL_FROM` | 送信元の Gmail アドレス | 推奨 |
| `MAIL_TO` | 宛先。カンマ区切りで複数可 | 任意 |

`MAIL_TO` 未設定時は `MAIL_FROM` と同じアドレスに送る。

> **既存の `~/wonderlabo/shift-watch/send-mail.py` は Keychain からパスワードを読むが、
> クラウド実行では Keychain を参照できない。** そのため同じアプリパスワードを
> `GMAIL_APP_PASSWORD` として Secrets に登録する必要がある。
> Keychain の値は次のコマンドで確認できる（画面に表示されるので取り扱い注意）:
>
> ```bash
> security find-generic-password -a <Gmailアドレス> -s wonder-gym-mail -w
> ```
>
> ローカルで実行する場合は Secrets 不要で、従来どおり Keychain から自動で読む。

### 2-3. ntfy（iPhoneプッシュ）

1. App Store で **ntfy** をインストール
2. 推測されにくいトピック名を決める（例: `yuzureru-saitama-7x4k2p`）
   トピック名を知っている人は誰でも購読・投稿できるので、必ずランダム文字列を含める
3. アプリで **Subscribe to topic** にそのトピック名を登録
4. 同じ文字列を Secrets の `NTFY_TOPIC` に登録

### 2-4. 疎通確認

Actions タブ → yuzureru-monitor → **Run workflow** で手動実行し、ログが
`state=NONE` になることを確認する。通知経路そのものの確認はローカルで：

```bash
cd ~/yuzureru-monitor && NTFY_TOPIC=あなたのトピック名 python3 monitor.py --test-notify
```

---

## 4. 使い方

```bash
python3 monitor.py                                   # 通常実行（通知あり）
python3 monitor.py --dry-run                         # 判定だけ。通知しない
python3 monitor.py --test-notify                     # 通知経路の疎通確認だけ
python3 monitor.py --fixture tests/fixture_available.html --dry-run   # 枠ありダミーで検知テスト
```

`tests/fixture_available.html` は実際の一覧HTMLの当該行だけを「残りわずか」に書き換えたもの。
ロジックを変更したら必ずこれで検知が発火することを確認すること。

---

## 5. 止め方・変え方

### ローカル版（現在稼働中）

```bash
# 状態を見る（数字が出ていれば登録されている）
launchctl list | grep yuzureru

# 一時停止
launchctl unload ~/Library/LaunchAgents/com.junsugata.yuzureru-monitor.plist

# 再開
launchctl load -w ~/Library/LaunchAgents/com.junsugata.yuzureru-monitor.plist

# 完全に消す（停止したうえで定義ファイルを削除）
launchctl unload ~/Library/LaunchAgents/com.junsugata.yuzureru-monitor.plist && rm ~/Library/LaunchAgents/com.junsugata.yuzureru-monitor.plist

# 今すぐ1回だけ動かす
launchctl start com.junsugata.yuzureru-monitor

# ログを見る
tail -20 ~/Library/Logs/yuzureru-monitor.log
```

| やりたいこと | 方法 |
|---|---|
| **間隔を変える** | plist の `StartCalendarInterval` を編集して再読み込み |
| **通知先を変える** | plist の `MAIL_TO` / `NTFY_TOPIC` を編集して再読み込み。コード変更不要 |
| **再通知間隔を変える** | `monitor.py` の `RENOTIFY_MINUTES`（既定60分） |

### GitHub Actions版（設定した場合）

| やりたいこと | 方法 |
|---|---|
| **一時停止** | Actions タブ → yuzureru-monitor → 右上「…」→ Disable workflow |
| **完全停止** | リポジトリを削除、または `.github/workflows/monitor.yml` を削除 |
| **間隔を変える** | `monitor.yml` の `cron` を編集（UTC表記。JST = UTC+9） |
| **通知先を変える** | Secrets の `NTFY_TOPIC` / `MAIL_TO` を書き換える |

### 期限切れの挙動

**2026年11月2日 23:59 JST** を過ぎると、監視をスキップして
「監視を終了しました」通知を **1回だけ** 送り、以降は何もしない。
RUNNET へのアクセスは一切発生しなくなるが、launchd（および Actions）のスケジュール自体は
空振りし続けるので、気になれば上記の unload / Disable workflow で完全に止めること。

---

## 6. 通知が来たときにやること

先着順なので速度が勝負。通知に直リンクが入っている。

1. リンクからエントリー画面へ（**RUNNET のログインが必要**）
2. 決済手段は **クレジットカード / PayPal / 全額RUNPO払いのみ**。コンビニ・ペイジー不可
3. **ゆずれ～る利用手数料 550円** が参加料15,000円とは別にかかる
4. 参加賞など一部サービスが通常エントリーと異なる場合がある

---

## 7. 設計メモ

- **ポーリング間隔**：日中5分／深夜15分。5分未満は実装しない。
  当初は日中15分だったが、実測で枠が15〜18分で消えることが分かったため下限まで詰めた（2026-08-20）。
  GitHub Actions の schedule は混雑時に数分〜十数分遅延するため、そちらを使う場合の実効間隔はこれより長くなる。
- **リクエスト数**：1回あたり通常2リクエスト（検索 + 該当ページ）。
  一覧の検索フィルタ（`competitionName`）はGET/POSTともサーバー側で無視されるため、
  ページ送りで該当行に到達している。ページ位置は `state.json` にキャッシュする。
  位置がずれた場合は後ろのページから順に自己修復的に再探索する（最悪13リクエスト）。
- **robots.txt**：対象パスは許可されている（Disallow は他の特定 raceId のみの列挙）。
  汎用UAへの Crawl-delay 指定もないが、自主的に間隔を空け、
  User-Agent に連絡先を入れ、リトライは指数バックオフ最大3回に制限している。
- **サイレント故障対策**：行が見つからない・バルーン画像が特定できない場合は即エラー通知を出す
  （同種エラーは3時間に1回まで）。
- **一時障害と本物の異常の区別**：ネットワーク到達不能（DNS失敗・タイムアウト）は
  ノートPCではスリープ復帰直後に日常的に起きるため、**3回連続で失敗するまで通知しない**
  （`NET_FAIL_THRESHOLD`）。毎回通知すると狼少年になり、本物のエラーが埋もれるため。
  構造変化の疑い（パース失敗）は1回目から即通知する。
- **ntfyのタイトル文字化け**：HTTPヘッダに非ASCIIは載せられない。パーセントエンコードだと
  端末に `%F0%9F%8F%83` のまま表示されてロック画面で判別不能になるため、
  **RFC 2047 encoded-word**（`=?UTF-8?B?...?=`）で送っている。実機配信して読み返し確認済み（2026-08-20）。
- **状態の紛失**：Actions のキャッシュが飛んで `state.json` を失っても、
  発生するのは「重複通知が1回増える」だけで、取りこぼしは起きない設計。

## 8. ファイル構成

```
monitor.py                      本体（標準ライブラリのみ。pip install 不要）
.github/workflows/monitor.yml   スケジュール実行（GitHub版・未使用）
state.json                      前回状態（自動生成）
snapshots/baseline_*.html       判定基準となった2026-08-16時点の実HTML
snapshots/page_*.html           実行時スナップショット（直近10件・gitignore）
tests/fixture_available.html    枠ありダミー（検知テスト用）
NTFY_TOPIC.txt                  ntfyトピック名の控え（gitignore・人に見せない）

~/Library/LaunchAgents/com.junsugata.yuzureru-monitor.plist   スケジュール定義（ローカル版）
~/Library/Logs/yuzureru-monitor.log                           実行ログ
```
