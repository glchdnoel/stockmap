#!/usr/bin/env python3
"""스톡맵 맥 중계 프로그램 — 국내 종목 실시간 시세 (조회 전용)

하는 일
  토스증권 Open API에서 국내 종목 현재가를 2초마다 받아서,
  이 맥에서 연 스톡맵 웹앱(glchdnoel.github.io/stockmap)에만 가격 숫자를 건네줘요.

보안 원칙
  1. 토스 키는 키체인에만 있고, 웹앱으로는 절대 보내지 않아요. 웹앱이 받는 건 종목 코드·가격·시각뿐이에요.
  2. 이 맥 안(127.0.0.1)에서만 열려요. 같은 와이파이의 다른 기기는 접속할 수 없어요.
  3. 요청마다 세 가지를 확인해요.
       - Host: 127.0.0.1 또는 localhost로 온 요청만 (DNS 리바인딩 공격 차단)
       - Origin: 스톡맵 주소에서 온 요청만 (다른 웹사이트가 몰래 부르는 것 차단)
       - 중계 토큰: 웹앱 설정에 넣은 비밀 토큰이 맞는지
  4. 토스에 보내는 호출은 토큰 발급, 현재가, 일봉(전일 종가용) 세 개뿐이에요.
     계좌·주문 호출은 코드에 없고, 다른 호출은 보내기 전에 막아요.
  5. 아무도 시세를 요청하지 않으면 30초 뒤 토스 호출을 멈춰요.

실행 (mac-sync 폴더에서)
  python3 stockmap_relay.py            # 시작. 처음엔 중계 토큰을 만들어 보여줘요
  python3 stockmap_relay.py --open     # 시작하고 크롬으로 스톡맵 열기 ('스톡맵 중계 켜기.command'가 이렇게 실행해요)
  python3 stockmap_relay.py token      # 중계 토큰 다시 보기
  python3 stockmap_relay.py new-token  # 중계 토큰 새로 만들기 (예전 토큰은 무효)
  끝내기: 터미널에서 Ctrl + C
"""
import argparse
import hmac
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stockmap_sync import BASE, ERROR_HELP, OPENER, SyncError, _error_code, keychain_get, write_file_safely

KST = timezone(timedelta(hours=9))
PORT = 18765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
DEFAULT_ORIGINS = {"https://glchdnoel.github.io"}
APP_URL = "https://glchdnoel.github.io/stockmap/"
APP_NAMES = ("스톡맵 - 내 포트폴리오", "스톡맵")  # 크롬 '앱으로 설치' 했을 때의 이름 (manifest의 name / short_name)
TOKEN_FILE = Path.home() / ".stockmap" / "relay-token"
TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")
SYMBOL_RE = re.compile(r"^[0-9A-Z]{6}$")      # 국내 종목 코드 6자리 (예: 000660, 0167A0)
MAX_SYMBOLS = 50
POLL_SECONDS = 2.0
IDLE_SECONDS = 30
PREV_CLOSE_REFRESH = 30 * 60
MAX_RESPONSE = 500_000
TOSS_ALLOWED = {("POST", "/oauth2/token"), ("GET", "/api/v1/prices"), ("GET", "/api/v1/candles")}


class RelayError(Exception):
    pass


# ---------------------------------------------------------------------------
# 중계 토큰 (웹앱과 이 프로그램만 아는 비밀값. 토스 키와는 상관없어요)
# ---------------------------------------------------------------------------
def load_or_create_token(force_new=False):
    if not force_new:
        try:
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if TOKEN_RE.match(t):
                return t, False
        except FileNotFoundError:
            pass
    t = secrets.token_hex(20)
    write_file_safely(TOKEN_FILE, t + "\n")  # 폴더 700, 파일 600
    return t, True


# ---------------------------------------------------------------------------
# 토스 호출 (허용된 세 개만, 리다이렉트 금지)
# ---------------------------------------------------------------------------
def toss_call(method, path, *, token=None, form=None, query=None):
    if (method, path) not in TOSS_ALLOWED:
        raise RelayError(f"허용되지 않은 호출이라 막았어요: {method} {path}")
    url = BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"Accept": "application/json", "User-Agent": "stockmap-sync/1.0"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=10) as res:
            body = res.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        code = _error_code(e.read(20000))
        err = RelayError(f"토스 응답 오류 {e.code} ({code or '코드 없음'}). {ERROR_HELP.get(code or '', '')}".strip())
        err.status, err.code = e.code, code
        raise err
    except urllib.error.URLError as e:
        raise RelayError(f"인터넷 연결 오류: {getattr(e, 'reason', e)}")
    except SyncError as e:  # 리다이렉트 차단
        raise RelayError(str(e))
    if len(body) > MAX_RESPONSE:
        raise RelayError("응답이 비정상적으로 커요.")
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise RelayError("응답 형식이 올바르지 않아요.")


def num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 1e10 else None


def parse_time(v):
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=KST)


def prev_close_from_candles(candles, price_day):
    """일봉(최신순)에서 '가격이 찍힌 날' 바로 전 거래일의 종가를 찾아요."""
    for c in candles:
        if not isinstance(c, dict):
            continue
        t = parse_time(c.get("timestamp"))
        close = num(c.get("closePrice"))
        if t and close and t.astimezone(KST).date() < price_day:
            return close
    return None


class TossPrices:
    """토스에서 가격을 받아 메모리에만 들고 있어요."""

    def __init__(self, call=toss_call, read_keychain=keychain_get, log=print):
        self.call, self.read_keychain, self.log = call, read_keychain, log
        self.lock = threading.Lock()
        self.token = None
        self.wanted = {}      # symbol -> 마지막으로 요청받은 시각
        self.prices = {}      # symbol -> {"price", "at"}
        self.prev = {}        # symbol -> {"close", "day", "fetched"}
        self.status = "대기 중"
        self.fetched_at = None

    def ensure_token(self):
        if self.token:
            return self.token
        cid, secret = self.read_keychain("client_id"), self.read_keychain("client_secret")
        if not cid or not secret:
            raise RelayError("키체인에 토스 키가 없어요. 'python3 stockmap_sync.py setup'을 먼저 하세요.")
        j = self.call("POST", "/oauth2/token", form={
            "grant_type": "client_credentials", "client_id": cid, "client_secret": secret})
        secret = None  # 토큰을 받았으면 바로 버려요 (만료되면 키체인에서 다시 읽어요)
        tok = j.get("access_token") if isinstance(j, dict) else None
        if not isinstance(tok, str) or not tok or len(tok) > 8192 or any(c.isspace() for c in tok):
            raise RelayError("토큰 응답이 올바르지 않아요.")
        self.token = tok
        return tok

    def _call_with_token(self, method, path, query):
        for attempt in range(2):
            tok = self.ensure_token()
            try:
                return self.call(method, path, token=tok, query=query)
            except RelayError as e:
                expired = getattr(e, "status", None) == 401 or getattr(e, "code", None) in ("expired-token", "token-revoked")
                if expired and attempt == 0:
                    self.token = None  # 만료·무효 → 한 번만 새로 받아요
                    continue
                raise

    def want(self, symbols):
        now = time.time()
        with self.lock:
            for s in symbols:
                self.wanted[s] = now
            # 오래 안 찾은 종목은 목록에서 빼요
            for s in [s for s, t in self.wanted.items() if now - t > IDLE_SECONDS]:
                del self.wanted[s]

    def active_symbols(self):
        now = time.time()
        with self.lock:
            return sorted(s for s, t in self.wanted.items() if now - t <= IDLE_SECONDS)[:MAX_SYMBOLS]

    def refresh_prices(self, symbols):
        j = self._call_with_token("GET", "/api/v1/prices", {"symbols": ",".join(symbols)})
        rows = j.get("result") if isinstance(j, dict) else None
        if not isinstance(rows, list):
            raise RelayError("현재가 응답 형식이 올바르지 않아요.")
        got = {}
        for r in rows[:MAX_SYMBOLS]:
            if not isinstance(r, dict) or r.get("symbol") not in symbols or r.get("currency") not in (None, "KRW"):
                continue
            p = num(r.get("lastPrice"))
            if p:
                t = parse_time(r.get("timestamp"))
                got[r["symbol"]] = {"price": p, "at": t.isoformat() if t else None}
        with self.lock:
            self.prices.update(got)
            self.fetched_at = datetime.now(KST).isoformat(timespec="seconds")

    def refresh_prev_close(self, symbol):
        with self.lock:
            p = self.prices.get(symbol)
            old = self.prev.get(symbol)
        t = parse_time(p["at"]) if p and p.get("at") else None
        day = (t or datetime.now(KST)).astimezone(KST).date()
        if old and old["day"] == day and time.time() - old["fetched"] < PREV_CLOSE_REFRESH:
            return
        j = self._call_with_token("GET", "/api/v1/candles", {"symbol": symbol, "interval": "1d", "count": 5})
        body = j.get("result", j) if isinstance(j, dict) else None
        candles = body.get("candles") if isinstance(body, dict) else None
        close = prev_close_from_candles(candles if isinstance(candles, list) else [], day)
        with self.lock:
            self.prev[symbol] = {"close": close, "day": day, "fetched": time.time()}

    def poll_once(self):
        symbols = self.active_symbols()
        if not symbols:
            self.status = "대기 중 (웹앱 요청 없음)"
            return
        try:
            self.refresh_prices(symbols)
            for s in symbols:
                self.refresh_prev_close(s)
            if not self.status.startswith("받는 중"):
                self.log(f"[{datetime.now(KST):%H:%M:%S}] 시세 받는 중: {', '.join(symbols)}")
            self.status = "받는 중"
        except RelayError as e:
            if self.status != str(e):
                self.log(f"[{datetime.now(KST):%H:%M:%S}] {e}")
            self.status = str(e)

    def snapshot(self, symbols):
        with self.lock:
            out = []
            for s in symbols:
                p = self.prices.get(s)
                if not p:
                    continue
                pc = self.prev.get(s)
                out.append({"symbol": s, "price": p["price"], "at": p["at"],
                            "prevClose": pc["close"] if pc else None})
            return {"prices": out, "fetchedAt": self.fetched_at,
                    "ok": self.status == "받는 중", "status": self.status if self.status != "받는 중" else ""}


# ---------------------------------------------------------------------------
# 이 맥 안에서만 여는 작은 서버
# ---------------------------------------------------------------------------
def make_handler(feed, relay_token, origins):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "stockmap-relay"
        sys_version = ""

        def log_message(self, *args):
            pass  # 요청 내용은 기록하지 않아요

        def _reject(self, code, why):
            self.server.rejected += 1
            if self.server.rejected in (1, 10, 100):
                print(f"[{datetime.now(KST):%H:%M:%S}] 거절한 요청 {self.server.rejected}건 (마지막 이유: {why})")
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _gate(self):
            """Host·Origin 확인. 통과하면 Origin을 돌려줘요."""
            if self.headers.get("Host", "") not in ALLOWED_HOSTS:
                self._reject(421, "Host가 이 맥 주소가 아님")
                return None
            origin = self.headers.get("Origin", "")
            if origin not in origins:
                self._reject(403, "허용되지 않은 웹사이트")
                return None
            return origin

        def _cors(self, origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

        def do_OPTIONS(self):
            origin = self._gate()
            if not origin:
                return
            self.send_response(204)
            self._cors(origin)
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.send_header("Access-Control-Allow-Headers", "X-Stockmap-Relay")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            origin = self._gate()
            if not origin:
                return
            given = self.headers.get("X-Stockmap-Relay", "")
            if not hmac.compare_digest(given.encode(), relay_token.encode()):
                self._reject(401, "중계 토큰이 틀림")
                return
            u = urllib.parse.urlsplit(self.path)
            if u.path != "/v1/prices":
                self._reject(404, "없는 주소")
                return
            raw = urllib.parse.parse_qs(u.query).get("symbols", [""])[0]
            symbols = [s for s in raw.split(",") if s][:MAX_SYMBOLS + 1]
            if not symbols or len(symbols) > MAX_SYMBOLS or not all(SYMBOL_RE.match(s) for s in symbols):
                self._reject(400, "종목 코드 형식 오류")
                return
            feed.want(symbols)
            body = json.dumps(feed.snapshot(symbols), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self._cors(origin)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self._reject(405, "허용되지 않은 방식")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def make_server(feed, relay_token, origins, port=PORT):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(feed, relay_token, origins))
    srv.daemon_threads = True
    srv.rejected = 0
    return srv


def poll_loop(feed, stop):
    while not stop.is_set():
        feed.poll_once()
        stop.wait(POLL_SECONDS)


def open_app():
    """크롬에 설치한 스톡맵 앱이 있으면 그걸, 없으면 크롬 탭으로 열어요.
    (사파리는 https 페이지에서 이 맥 주소로 가는 요청을 막을 수 있어서 크롬만 써요)"""
    for name in APP_NAMES:
        if subprocess.run(["/usr/bin/open", "-a", name], capture_output=True).returncode == 0:
            return name
    if subprocess.run(["/usr/bin/open", "-a", "Google Chrome", APP_URL], capture_output=True).returncode == 0:
        return "Google Chrome"
    print(f"크롬을 찾지 못했어요. 크롬에서 직접 여세요: {APP_URL}")
    return None


def main(argv=None):
    if sys.platform != "darwin" and os.environ.get("STOCKMAP_SYNC_TEST") != "1":
        print("이 프로그램은 macOS 전용이에요 (키체인 사용).")
        return 2
    p = argparse.ArgumentParser(description="스톡맵 맥 중계 프로그램 (국내 종목 시세, 조회 전용)")
    p.add_argument("command", nargs="?", default="run", choices=["run", "token", "new-token"])
    p.add_argument("--open", action="store_true", help="중계가 켜지면 크롬으로 스톡맵을 열어요")
    p.add_argument("--allow-origin", action="append", default=[],
                   help="테스트용으로 허용할 웹앱 주소 추가 (예: http://localhost:8765)")
    a = p.parse_args(argv)
    origins = set(DEFAULT_ORIGINS)
    for o in a.allow_origin:
        if not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$", o):
            print(f"주소 형식이 올바르지 않아요: {o}")
            return 2
        origins.add(o)
    try:
        token, created = load_or_create_token(force_new=(a.command == "new-token"))
    except (OSError, SyncError) as e:
        print(f"중계 토큰 파일을 만들지 못했어요: {e}")
        return 1
    if a.command in ("token", "new-token") or created:
        print("\n중계 토큰 (스톡맵 설정 → '맥 중계'에 붙여넣으세요):")
        print(f"  {token}")
        print("이 값은 시세 숫자만 받을 수 있는 열쇠예요. 토스 키가 아니에요.\n")
        if a.command != "run":
            return 0

    feed = TossPrices()
    try:
        feed.ensure_token()  # 시작할 때 한 번 키체인을 열어요 (허락 창에서는 '허용'만)
    except RelayError as e:
        print(f"중단: {e}")
        return 1
    try:
        srv = make_server(feed, token, origins)
    except OSError as e:
        print(f"포트 {PORT}을(를) 열지 못했어요. 이미 실행 중인지 확인하세요. ({e})")
        return 1
    stop = threading.Event()
    threading.Thread(target=poll_loop, args=(feed, stop), daemon=True).start()
    print(f"중계 시작: http://127.0.0.1:{PORT} (이 맥 안에서만)")
    print("스톡맵을 열어 두면 2초마다 국내 종목 가격을 받아요. 끝내려면 Ctrl + C (또는 이 창 닫기)")
    if a.open:
        open_app()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n중계를 끝냈어요.")
    finally:
        stop.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
