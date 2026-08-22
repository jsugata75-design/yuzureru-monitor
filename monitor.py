#!/usr/bin/env python3
"""さいたまマラソン2027 ゆずれ～る枠 監視スクリプト.

RUNNET の「ゆずれ～る実施大会一覧」から対象大会の行を探し、
空き状況バルーン画像の変化を検知して通知する。

判定シグナル(検証済み):
    balloon_yuzureru_yoko.png    -> BEFORE    ゆずれ～る開始前
    balloon_yuzureru_none.png    -> NONE      期間中だが空きなし
    balloon_yuzureru_sankaku.png -> AVAILABLE エントリー可・残りわずか
    上記以外の balyuzureru 画像   -> AVAILABLE (フェイルオープン。取りこぼし防止)

大会詳細ページの緑ボタン(class="btEntryMainYuzureru")は
「定員締切済みでゆずれ～るが唯一の入口」を示すだけで空き枠とは無関係。
判定に使ってはいけない(2026-08-16 に対照実験で確認済み)。

エントリー自体はログインが必要なため、本スクリプトは通知までを行う。
認証・自動入力・決済は一切行わない。
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

# ---------------------------------------------------------------- 設定

RACE_ID = "392524"
RACE_NAME = "さいたまマラソン2027 マラソンの部"

JST = timezone(timedelta(hours=9))
DEADLINE = datetime(2026, 11, 2, 23, 59, 59, tzinfo=JST)  # ゆずれ～る期間終了

BASE = "https://runnet.jp/entry/runtes/user/pc/"
SEARCH_URL = BASE + "RaceSearchZZSDetailAction.do?command=search&serviceType=507"
PAGE_URL = BASE + "RaceSearchZZSDetailAction.do?command=page&&specialize=null&pageIndex={n}"
ENTRY_URL = BASE + f"competitionDetailAfterAction.do?raceId={RACE_ID}&div=5"
DETAIL_URL = BASE + f"competitionDetailAction.do?div=1&raceId={RACE_ID}"

# 公開リポジトリに個人情報を残さないため、連絡先入りUAは環境変数で与える
UA = os.environ.get("MONITOR_UA", "yuzureru-monitor/1.0 (personal use)")

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
SNAP_DIR = ROOT / "snapshots"
SNAP_KEEP = 10

NET_FAIL_THRESHOLD = 3     # ネットワーク不通が何回連続したら通知するか
RENOTIFY_MINUTES = 60      # 枠ありが続く間の再通知間隔
ERROR_COOLDOWN_HOURS = 3   # 同種エラーの再通知抑止
REQUEST_GAP_SEC = 2        # ページ送り間の待機
MAX_RETRIES = 3

# 状態ラベル
AVAILABLE, NONE, BEFORE, UNKNOWN = "AVAILABLE", "NONE", "BEFORE", "UNKNOWN"


class TransientError(RuntimeError):
    """ネットワーク到達不能など、放っておけば直る一時障害.

    ノートPCではスリープ復帰直後にWi-Fi接続前の実行が起きてDNSが引けない。
    これを毎回通知すると本物のエラーが埋もれるため、構造変化とは別扱いにする。
    """


# ---------------------------------------------------------------- 取得

def _opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [
        ("User-Agent", UA),
        ("Accept-Language", "ja,en;q=0.8"),
    ]
    return op


def fetch(op, url: str) -> tuple[str, int]:
    """指数バックオフ付きで取得。(html, status) を返す。"""
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            with op.open(url, timeout=30) as r:
                raw = r.read()
                return raw.decode("utf-8", errors="replace"), r.status
        except OSError as e:  # URLError/HTTPError/timeout/SSLError はすべて OSError の系統
            last = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 2)
    raise TransientError(f"取得失敗 ({MAX_RETRIES}回リトライ): {url} -> {last}")


# ---------------------------------------------------------------- 解析

ROW_SPLIT = re.compile(r'class="item-title"')
RACE_RE = re.compile(r"raceId=(\d+)&div=1")
BALLOON_RE = re.compile(r'<img[^>]*balyuzureru[^>]*>')
SRC_RE = re.compile(r'src="([^"]*)"')
PAGEIDX_RE = re.compile(r"pageIndex=(\d+)")


def parse_rows(html: str) -> dict[str, str | None]:
    """一覧HTMLを行単位に切り、raceId -> バルーン画像src(なければNone) を返す。"""
    cuts = [m.start() for m in ROW_SPLIT.finditer(html)]
    rows: dict[str, str | None] = {}
    for i, c in enumerate(cuts):
        seg = html[c: cuts[i + 1] if i + 1 < len(cuts) else len(html)]
        rid = RACE_RE.search(seg)
        if not rid:
            continue
        bal = BALLOON_RE.search(seg)
        src = None
        if bal:
            m = SRC_RE.search(bal.group(0))
            src = m.group(1) if m else None
        rows.setdefault(rid.group(1), src)
    return rows


def classify(src: str | None) -> str:
    if not src:
        return UNKNOWN
    if "balloon_yuzureru_none" in src:
        return NONE
    if "balloon_yuzureru_yoko" in src:
        return BEFORE
    # sankaku(残りわずか) および未知の画像はすべて枠ありとみなす(フェイルオープン)
    return AVAILABLE


def max_page(html: str) -> int:
    idx = [int(m.group(1)) for m in PAGEIDX_RE.finditer(html)]
    return max(idx) if idx else 1


def locate_race(op, cached_page: int | None) -> tuple[str | None, int, str, int]:
    """対象大会を探す。(balloon_src, page_index, html, requests_used) を返す。"""
    html, _ = fetch(op, SEARCH_URL)
    used = 1
    rows = parse_rows(html)
    if not rows:
        raise RuntimeError("一覧の行を1件も解析できません(ページ構造が変わった可能性)")
    if RACE_ID in rows:
        return rows[RACE_ID], 1, html, used

    last = max_page(html)
    # キャッシュ済みページ -> 後ろのページから順に(対象は開催日が遠く末尾寄り) -> 残り
    order: list[int] = []
    if cached_page and 2 <= cached_page <= last:
        order.append(cached_page)
    order += [p for p in range(last, 1, -1)]
    seen = set()
    for p in order:
        if p in seen:
            continue
        seen.add(p)
        time.sleep(REQUEST_GAP_SEC)
        page_html, _ = fetch(op, PAGE_URL.format(n=p))
        used += 1
        prows = parse_rows(page_html)
        if RACE_ID in prows:
            return prows[RACE_ID], p, page_html, used
    raise RuntimeError(
        f"raceId={RACE_ID} が一覧{last}ページ中に見つかりません"
        "(ゆずれ～る対象から外れた/構造変化の可能性)"
    )


# ---------------------------------------------------------------- 通知

def _header_encode(s: str) -> str:
    """HTTPヘッダは非ASCIIを載せられないので RFC 2047 encoded-word にする。

    パーセントエンコードだと端末に %F0%9F%8F%83 のまま表示されてしまい、
    ロック画面で何の通知か分からなくなる(2026-08-20 に実機向け配信で確認)。
    """
    return s if s.isascii() else "=?UTF-8?B?" + base64.b64encode(s.encode()).decode() + "?="


def notify_ntfy(title: str, body: str, priority: str, tags: str) -> str:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return "ntfy: NTFY_TOPIC 未設定のためスキップ"
    url = f"https://ntfy.sh/{topic}"
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": _header_encode(title),
            "Priority": priority,
            "Tags": tags,
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return f"ntfy: OK ({r.status})"
    except Exception as e:
        return f"ntfy: 送信失敗: {e}"


def _gmail_password(addr: str) -> str | None:
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if pw:
        return pw
    # ローカル実行時は既存 wonder-gym-mail の Keychain を流用(クラウドでは参照できない)
    if not addr:
        return None
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-a", addr,
             "-s", os.environ.get("KEYCHAIN_SERVICE", "wonder-gym-mail"), "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return None


def notify_mail(subject: str, body: str) -> str:
    addr = os.environ.get("MAIL_FROM", "").strip()
    to = [x.strip() for x in os.environ.get("MAIL_TO", addr).split(",") if x.strip()]
    if not addr or not to:
        return "mail: MAIL_FROM / MAIL_TO 未設定のためスキップ"
    pw = _gmail_password(addr)
    if not pw:
        return "mail: 認証情報なし(GMAIL_APP_PASSWORD / Keychain)のためスキップ"
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(addr, pw)
            s.sendmail(addr, to, msg.as_string())
        return f"mail: OK -> {', '.join(to)}"
    except Exception as e:
        return f"mail: 送信失敗: {e}"


def notify(title: str, body: str, *, urgent: bool, dry_run: bool) -> list[str]:
    if dry_run:
        return [f"[dry-run] 通知抑止: {title}"]
    pri = "urgent" if urgent else "default"
    tags = "rotating_light" if urgent else "warning"
    return [notify_ntfy(title, body, pri, tags), notify_mail(title, body)]


def slot_message(now: datetime, page: int, src: str | None) -> tuple[str, str]:
    title = "🏃 ゆずれ～る枠が出ました（さいたまマラソン2027）"
    body = (
        f"{RACE_NAME} に空き枠が出ました。先着順です。今すぐエントリーしてください。\n\n"
        f"検知時刻: {now.strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"表示状態: {'残りわずか(▲)' if src and 'sankaku' in src else '空きあり'}\n"
        f"（正確な残枠数はログインしないと取得できないため表示のみ）\n\n"
        f"▼ エントリー画面（ログインが必要です）\n{ENTRY_URL}\n\n"
        f"▼ 大会詳細\n{DETAIL_URL}\n\n"
        "── 決済の注意 ──\n"
        "・クレジットカード / PayPal / 全額RUNPO払いのみ。コンビニ・ペイジー不可\n"
        "・ゆずれ～る利用手数料 550円が別途かかります\n"
        "・参加賞など一部サービスが通常エントリーと異なる場合があります\n"
        f"（一覧{page}ページ目 / 検知元: {src or 'n/a'}）"
    )
    return title, body


# ---------------------------------------------------------------- 状態

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def save_snapshot(html: str, now: datetime) -> None:
    SNAP_DIR.mkdir(exist_ok=True)
    (SNAP_DIR / f"page_{now.strftime('%Y%m%d_%H%M%S')}.html").write_text(html, encoding="utf-8")
    snaps = sorted(SNAP_DIR.glob("page_*.html"))
    for old in snaps[:-SNAP_KEEP]:
        old.unlink()


def parse_dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------- 本体

def main() -> int:
    ap = argparse.ArgumentParser(description="さいたまマラソン2027 ゆずれ～る枠 監視")
    ap.add_argument("--dry-run", action="store_true", help="通知を送らず判定だけ行う")
    ap.add_argument("--test-notify", action="store_true", help="通知経路の疎通確認のみ")
    ap.add_argument("--fixture", metavar="PATH", help="ローカルHTMLを一覧ページとして判定(ダミーテスト用)")
    args = ap.parse_args()

    now = datetime.now(JST)
    st = load_state()

    if args.test_notify:
        body = f"通知経路の疎通確認です。\n{now.strftime('%Y-%m-%d %H:%M:%S')} JST\n監視対象: {RACE_NAME}"
        for line in notify("✅ ゆずれ～る監視 テスト通知", body, urgent=False, dry_run=False):
            print(line)
        return 0

    # 期間終了チェック
    if now > DEADLINE:
        if not st.get("deadline_notified"):
            body = (
                f"ゆずれ～るエントリー期間（〜{DEADLINE.strftime('%Y-%m-%d %H:%M')} JST）が終了したため、"
                "監視を終了します。\nGitHub Actions のスケジュールを無効化してください（README参照）。"
            )
            for line in notify("🏁 ゆずれ～る監視を終了しました", body, urgent=False, dry_run=args.dry_run):
                print(line)
            st["deadline_notified"] = True
            save_state(st)
        print(f"[{now:%F %T}] 期間終了のためスキップ")
        return 0

    prev = st.get("last_state")
    op = _opener()

    # --- 取得と判定
    try:
        if args.fixture:
            html = Path(args.fixture).read_text(encoding="utf-8", errors="replace")
            rows = parse_rows(html)
            if RACE_ID not in rows:
                raise RuntimeError(f"fixture に raceId={RACE_ID} の行がありません")
            src, page, used = rows[RACE_ID], 0, 0
        else:
            src, page, html, used = locate_race(op, st.get("page_index"))
        state = classify(src)
    except Exception as e:
        # 一時的なネット不通(スリープ復帰直後など)と、本物の異常(構造変化)を区別する。
        # 前者を毎回通知すると狼少年になり、本物のエラーが埋もれる。
        transient = isinstance(e, (TransientError, OSError))
        fails = st.get("consecutive_net_failures", 0) + 1 if transient else 0
        st["consecutive_net_failures"] = fails
        last_err = parse_dt(st.get("last_error_notified_at"))
        cooled = (not last_err) or (now - last_err > timedelta(hours=ERROR_COOLDOWN_HOURS))
        worth_notifying = (not transient) or fails >= NET_FAIL_THRESHOLD
        tag = f"(一時障害 {fails}回連続)" if transient else "(構造変化の疑い)"
        print(f"[{now:%F %T}] ERROR{tag}: {e}", file=sys.stderr)
        if cooled and worth_notifying:
            reason = (
                f"ネットワークに{fails}回連続で到達できていません。"
                "Wi-Fiが切れているか、RUNNET側が落ちている可能性があります。"
                if transient else
                "ページの構造が変わった可能性があります。判定ロジックの見直しが必要です。"
            )
            body = (
                f"ゆずれ～る監視でエラーが発生しました。監視が止まっている可能性があります。\n\n"
                f"{reason}\n\n"
                f"時刻: {now.strftime('%Y-%m-%d %H:%M:%S')} JST\n内容: {e}\n\n"
                f"一覧: {SEARCH_URL}\n手動確認: {DETAIL_URL}"
            )
            for line in notify("⚠️ ゆずれ～る監視エラー", body, urgent=False, dry_run=args.dry_run):
                print(line)
            st["last_error_notified_at"] = now.isoformat()
        st.setdefault("history", []).insert(0, {"at": now.isoformat(), "state": "ERROR", "detail": str(e)})
        st["history"] = st["history"][:20]
        save_state(st)
        return 1

    print(f"[{now:%F %T}] state={state} (prev={prev}) page={page} req={used} src={src}")

    if not args.fixture:
        st["page_index"] = page
        save_snapshot(html, now)
    st["last_error_notified_at"] = None
    st["consecutive_net_failures"] = 0

    # --- 通知判定
    sent: list[str] = []
    if state == AVAILABLE:
        last_notif = parse_dt(st.get("last_notified_at"))
        due = (prev != AVAILABLE) or (not last_notif) or \
              (now - last_notif >= timedelta(minutes=RENOTIFY_MINUTES))
        if due:
            title, body = slot_message(now, page, src)
            sent = notify(title, body, urgent=True, dry_run=args.dry_run)
            if not args.dry_run:
                st["last_notified_at"] = now.isoformat()
        else:
            sent = [f"枠あり継続中（前回通知から{RENOTIFY_MINUTES}分未経過のため抑止）"]
    elif state == UNKNOWN:
        body = (
            "対象大会の行は見つかりましたが、空き状況バルーン画像を特定できませんでした。\n"
            "RUNNET側のHTML構造が変わった可能性があります。手動で確認してください。\n\n"
            f"時刻: {now.strftime('%Y-%m-%d %H:%M:%S')} JST\n{DETAIL_URL}"
        )
        sent = notify("⚠️ ゆずれ～る監視: 構造変化の可能性", body, urgent=False, dry_run=args.dry_run)

    for line in sent:
        print(line)

    st["last_state"] = state
    st.setdefault("history", []).insert(0, {"at": now.isoformat(), "state": state, "page": page})
    st["history"] = st["history"][:20]
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
