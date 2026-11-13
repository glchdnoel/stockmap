"""stockmap_relay 모의 점검 (실제 토스증권·키체인에 접속하지 않음). 실행: python3 test_relay.py"""
import http.client
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

os.environ["STOCKMAP_SYNC_TEST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stockmap_relay as r  # noqa: E402

TOKEN = "a" * 40
ORIGIN = "https://glchdnoel.github.io"


class FakeToss:
    """토스 서버 흉내. 받은 호출을 기록해요."""

    def __init__(self, daily_page_limit=10, reject_adjusted=False):
        self.calls = []
        self.fail_prices_once = None
        self.daily_page_limit = daily_page_limit
        self.reject_adjusted = reject_adjusted

    def __call__(self, method, path, *, token=None, form=None, query=None, account_seq=None):
        self.calls.append((method, path, token, dict(query or {}), dict(form or {}), account_seq))
        if path == "/api/v1/market-indicators/prices":
            return {"result": [{"symbol": "KOSPI", "timestamp": "2026-11-13T15:30:00+09:00", "lastPrice": "2812.45"}]}
        if path == "/api/v1/market-indicators/KOSPI/candles":
            return {"result": {"candles": [
                {"timestamp": "2026-11-13T00:00:00+09:00", "closePrice": "2812.45"},
                {"timestamp": "2026-11-12T00:00:00+09:00", "closePrice": "2800.00"}], "nextBefore": None}}
        if path == "/api/v1/accounts":
            return {"result": [{"accountNo": "123-45-678901", "accountSeq": 1, "accountType": "BROKERAGE"},
                               {"accountNo": "999-99-999999", "accountSeq": 2, "accountType": "ISA"}]}
        if path == "/api/v1/holdings":
            items = {1: [{"symbol": "000660", "name": "SK하이닉스", "marketCountry": "KR", "currency": "KRW",
                          "quantity": "1", "averagePurchasePrice": "1300000", "lastPrice": "1233000"}],
                     2: [{"symbol": "000660", "name": "SK하이닉스", "marketCountry": "KR", "currency": "KRW",
                          "quantity": "1", "averagePurchasePrice": "1100000", "lastPrice": "1233000"},
                         {"symbol": "VOO", "marketCountry": "US", "currency": "USD",
                          "quantity": "2", "averagePurchasePrice": "500", "lastPrice": "520"}]}[account_seq]
            return {"result": {"items": items}}
        if path == "/oauth2/token":
            return {"access_token": f"jwt{len(self.calls)}", "token_type": "Bearer", "expires_in": 86400}
        if path == "/api/v1/prices":
            if self.fail_prices_once:
                e, self.fail_prices_once = self.fail_prices_once, None
                raise e
            out = []
            for s in query["symbols"].split(","):
                out.append({"symbol": s, "timestamp": "2026-11-13T19:59:59+09:00", "lastPrice": "1233000", "currency": "KRW"})
            out.append({"symbol": "999999", "lastPrice": "1", "currency": "KRW"})  # 요청 안 한 종목 → 무시
            return {"result": out}
        if path == "/api/v1/candles" and query.get("interval") == "1m":
            return {"result": {"candles": [
                {"timestamp": "2026-11-13T10:59:00Z", "closePrice": "1233000"},   # UTC → 19:59 KST
                {"timestamp": "2026-11-13T09:59:00Z", "closePrice": "1230000"},
            ], "nextBefore": "2026-11-13T09:58:00Z"}}
        if path == "/api/v1/candles" and query.get("count") == 200:
            self.daily_pages = getattr(self, "daily_pages", 0) + 1
            if self.reject_adjusted and query.get("adjusted"):
                e = r.RelayError("bad param"); e.status, e.code = 400, "invalid-parameter"
                raise e
            page = self.daily_pages
            from datetime import date as _d, timedelta as _td
            end = _d(2026, 11, 13) - _td(days=200 * (page - 1))
            candles = [{"timestamp": f"{(end - _td(days=i)).isoformat()}T00:00:00+09:00", "closePrice": str(1000 + page * 1000 + i)}
                       for i in range(200)]
            more = page < self.daily_page_limit
            return {"result": {"candles": candles, "nextBefore": f"{(end - _td(days=200)).isoformat()}T00:00:00+09:00" if more else None}}
        if path == "/api/v1/candles":
            return {"result": {"candles": [
                {"timestamp": "2026-11-13T00:00:00+09:00", "closePrice": "1233000"},
                {"timestamp": "2026-11-12T00:00:00+09:00", "closePrice": "1200000"},
                {"timestamp": "2026-11-11T00:00:00+09:00", "closePrice": "1190000"},
            ], "nextBefore": None}}
        raise AssertionError("예상 못 한 호출 " + path)


def keychain():
    return "cid", "csecret"


class RelayHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.toss = FakeToss()
        cls.feed = r.TossPrices(call=cls.toss, load_creds=keychain, log=lambda *a: None)
        cls.srv = r.make_server(cls.feed, TOKEN, {ORIGIN}, port=0)
        cls.port = cls.srv.server_address[1]
        r.ALLOWED_HOSTS.update({f"127.0.0.1:{cls.port}", f"localhost:{cls.port}"})
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method="GET", path="/v1/prices?symbols=000660", headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}", "Origin": ORIGIN, "X-Stockmap-Relay": TOKEN}
        h.update(headers or {})
        for k in [k for k, v in h.items() if v is None]:
            del h[k]
        c.putrequest(method, path, skip_host=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders()
        res = c.getresponse()
        body = res.read()
        c.close()
        return res, body

    def test_ok_flow(self):
        res, _ = self.req(path="/v1/prices?symbols=000660,0167A0")
        self.assertEqual(res.status, 200)
        self.feed.poll_once()
        res, body = self.req(path="/v1/prices?symbols=000660,0167A0")
        j = json.loads(body)
        self.assertEqual(res.getheader("Access-Control-Allow-Origin"), ORIGIN)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        self.assertTrue(j["ok"])
        self.assertEqual({p["symbol"] for p in j["prices"]}, {"000660", "0167A0"})
        p = j["prices"][0]
        self.assertEqual(p["price"], 1233000)
        self.assertEqual(p["prevClose"], 1200000)  # 가격 찍힌 날(11/13) 바로 전 거래일
        self.assertEqual(set(p), {"symbol", "price", "at", "prevClose"})  # 가격 정보만 나가요

    def test_response_has_no_secrets(self):
        self.req()
        self.feed.poll_once()
        _, body = self.req()
        text = body.decode()
        for secret in ("cid", "csecret", "jwt"):
            self.assertNotIn(secret, text)

    def test_wrong_host_rejected(self):  # DNS 리바인딩
        res, body = self.req(host=f"evil.example:{self.port}")
        self.assertEqual(res.status, 421)
        self.assertEqual(body, b"")

    def test_wrong_origin_rejected(self):
        for o in ("https://evil.example", "https://glchdnoel.github.io.evil.example", "null", None):
            res, body = self.req(headers={"Origin": o})
            self.assertEqual(res.status, 403, o)
            self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))

    def test_wrong_token_rejected(self):
        for t in ("b" * 40, "", TOKEN[:-1], None):
            res, _ = self.req(headers={"X-Stockmap-Relay": t})
            self.assertEqual(res.status, 401, t)

    def test_bad_symbols_rejected(self):
        for q in ("symbols=AAPL", "symbols=00066", "symbols=000660;rm",
                  "symbols=" + ",".join(f"{i:06d}" for i in range(51))):
            res, _ = self.req(path="/v1/prices?" + q)
            self.assertEqual(res.status, 400, q)

    def test_other_paths_and_methods_rejected(self):
        self.assertEqual(self.req(path="/v1/orders")[0].status, 404)
        self.assertEqual(self.req(path="/")[0].status, 404)
        self.assertEqual(self.req(method="POST")[0].status, 405)
        self.assertEqual(self.req(method="DELETE")[0].status, 405)

    def test_history_endpoint(self):
        res, body = self.req(path="/v1/history?symbol=000660&kind=intraday")
        self.assertEqual(res.status, 200)
        j = json.loads(body)
        self.assertEqual(j["values"][0], {"datetime": "2026-11-13 19:59:00", "close": 1233000})
        for q in ("symbol=000660&kind=hack", "symbol=AAPL&kind=daily", "kind=daily", "symbol=000660%0A&kind=daily"):
            self.assertEqual(self.req(path="/v1/history?" + q)[0].status, 400, q)
        self.assertEqual(self.req(path="/v1/history?symbol=000660&kind=daily", headers={"X-Stockmap-Relay": "b" * 40})[0].status, 401)

    def test_index_only_request(self):
        res, _ = self.req(path="/v1/prices?symbols=")
        self.assertEqual(res.status, 200)               # 종목 없이 코스피만 요청해도 돼요
        self.feed.poll_once()
        _, body = self.req(path="/v1/prices")
        j = json.loads(body)
        self.assertEqual(j["indices"], [{"symbol": "KOSPI", "price": 2812.45, "at": "2026-11-13T15:30:00+09:00", "prevClose": 2800.0}])

    def test_holdings_endpoint(self):
        with mock.patch.object(r.time, "sleep", lambda s: None):
            res, body = self.req(path="/v1/holdings")
        self.assertEqual(res.status, 200)
        j = json.loads(body)
        self.assertTrue(j["ok"])
        by = {h["symbol"]: h for h in j["holdings"]}
        self.assertEqual(by["000660"]["shares"], 2.0)          # 두 계좌 합침
        self.assertEqual(by["000660"]["avgPrice"], 1200000.0)  # 가중 평균
        self.assertEqual(by["VOO"]["currency"], "USD")
        self.assertNotIn("123-45", body.decode())               # 계좌번호는 안 나가요
        self.assertNotIn("accountNo", body.decode())
        self.assertEqual(self.req(path="/v1/holdings", headers={"X-Stockmap-Relay": "b" * 40})[0].status, 401)

    def test_preflight(self):
        res, _ = self.req(method="OPTIONS", headers={"X-Stockmap-Relay": None,
                                                     "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(res.status, 204)
        self.assertEqual(res.getheader("Access-Control-Allow-Origin"), ORIGIN)
        self.assertEqual(res.getheader("Access-Control-Allow-Private-Network"), "true")
        self.assertEqual(res.getheader("Access-Control-Allow-Methods"), "GET")
        res, _ = self.req(method="OPTIONS", headers={"Origin": "https://evil.example"})
        self.assertEqual(res.status, 403)


class FeedTest(unittest.TestCase):
    def make(self):
        toss = FakeToss()
        return toss, r.TossPrices(call=toss, load_creds=keychain, log=lambda *a: None)

    def test_only_allowed_toss_calls(self):
        toss, feed = self.make()
        feed.want(["000660"])
        feed.poll_once()
        feed.poll_once()
        paths = {c[1] for c in toss.calls}
        self.assertEqual(paths, {"/oauth2/token", "/api/v1/prices", "/api/v1/candles",
                                 "/api/v1/market-indicators/prices", "/api/v1/market-indicators/KOSPI/candles"})
        self.assertEqual(len([c for c in toss.calls if c[1] == "/oauth2/token"]), 1)  # 토큰 재사용
        self.assertEqual(len([c for c in toss.calls if c[1] == "/api/v1/candles"]), 1)  # 전일 종가는 캐시
        self.assertTrue(all(c[2] == "jwt1" for c in toss.calls if c[1] != "/oauth2/token"))

    def test_daily_history_paging_and_cache(self):
        toss, feed = self.make()
        h = feed.history("000660", "daily")
        self.assertTrue(h["ok"] and h["full"])
        self.assertEqual(toss.daily_pages, 4)                 # 200개씩 4번 = 800일
        self.assertEqual(len(h["values"]), 800)
        self.assertEqual(h["values"][0]["datetime"], "2026-11-13")  # 최신순
        self.assertTrue(all(c[3].get("adjusted") == "true" for c in toss.calls if c[3].get("count") == 200))
        befores = [c[3].get("before") for c in toss.calls if c[3].get("count") == 200]
        self.assertEqual(befores[0], None)
        self.assertTrue(all(befores[1:]))                     # nextBefore로 이어 받기
        feed.history("000660", "daily")
        self.assertEqual(toss.daily_pages, 4)                 # 6시간 캐시

    def test_daily_history_short_listing(self):
        toss = FakeToss(daily_page_limit=2)
        feed = r.TossPrices(call=toss, load_creds=keychain, log=lambda *a: None)
        h = feed.history("000660", "daily")
        self.assertEqual(toss.daily_pages, 2)
        self.assertTrue(h["full"])                            # 상장일까지 다 받음 → '기간 부족' 판단 가능

    def test_daily_adjusted_fallback(self):
        toss = FakeToss(reject_adjusted=True)
        feed = r.TossPrices(call=toss, load_creds=keychain, log=lambda *a: None)
        h = feed.history("000660", "daily")
        self.assertTrue(h["ok"])
        self.assertEqual(len(h["values"]), 800)

    def test_history_error_reported(self):
        def call(method, path, **kw):
            if path == "/oauth2/token":
                return {"access_token": "jwt"}
            e = r.RelayError("허용 IP 아님"); e.status, e.code = 403, "ip-not-allowed"
            raise e
        feed = r.TossPrices(call=call, load_creds=keychain, log=lambda *a: None)
        h = feed.history("000660", "daily")
        self.assertFalse(h["ok"])
        self.assertEqual(h["values"], [])

    def test_toss_call_blocks_other_paths(self):
        for m, p in (("GET", "/api/v1/assets"), ("POST", "/api/v1/accounts"), ("POST", "/api/v1/orders")):
            with self.assertRaises(r.RelayError):
                r.toss_call(m, p, token="t")

    def test_holdings_cached_and_account_header(self):
        toss, feed = self.make()
        with mock.patch.object(r.time, "sleep", lambda s: None):
            feed.holdings(); feed.holdings()
        seqs = [c[5] for c in toss.calls if c[1] == "/api/v1/holdings"]
        self.assertEqual(seqs, [1, 2])                     # 1분 캐시 → 두 번째는 토스 호출 없음

    def test_toss_call_sends_account_header(self):
        seen = {}
        class Res(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def fake_open(req, timeout=None):
            seen.update(dict(req.header_items()))
            return Res(b'{"result": {"items": []}}')
        with mock.patch.object(r.OPENER, "open", fake_open):
            r.toss_call("GET", "/api/v1/holdings", token="t", account_seq=3)
        self.assertEqual(seen.get("X-tossinvest-account"), "3")

    def test_orders_still_blocked(self):
        for m, p in (("POST", "/api/v1/orders"), ("GET", "/api/v1/orders"), ("DELETE", "/api/v1/orders/1"),
                     ("GET", "/api/v1/market-indicators/KOSDAQ/candles")):
            with self.assertRaises(r.RelayError):
                r.toss_call(m, p, token="t")

    def test_idle_stops_calls(self):
        toss, feed = self.make()
        feed.want(["000660"])
        with mock.patch.object(r.time, "time", lambda: 10**10):
            feed.poll_once()
        self.assertEqual(toss.calls, [])
        self.assertIn("대기", feed.status)

    def test_expired_token_reissued_once(self):
        toss, feed = self.make()
        feed.want(["000660"])
        feed.poll_once()
        e = r.RelayError("토큰 만료")
        e.status, e.code = 401, "expired-token"
        toss.fail_prices_once = e
        feed.poll_once()
        self.assertEqual(len([c for c in toss.calls if c[1] == "/oauth2/token"]), 2)
        self.assertEqual(feed.status, "받는 중")

    def test_ip_error_reported(self):
        toss, feed = self.make()
        feed.want(["000660"])
        e = r.RelayError("허용 IP 아님")
        e.status, e.code = 403, "ip-not-allowed"
        toss.fail_prices_once = e
        feed.poll_once()
        self.assertEqual(feed.status, "허용 IP 아님")
        self.assertFalse(feed.snapshot(["000660"])["ok"])

    def test_prev_close_rules(self):
        c = [{"timestamp": "2026-11-13T00:00:00+09:00", "closePrice": "13"},
             {"timestamp": "2026-11-12T00:00:00+09:00", "closePrice": "12"},
             {"timestamp": "bad", "closePrice": "1"}, "junk"]
        self.assertEqual(r.prev_close_from_candles(c, date(2026, 11, 13)), 12)  # 장중·장 마감 후
        self.assertEqual(r.prev_close_from_candles(c, date(2026, 11, 14)), 13)
        self.assertIsNone(r.prev_close_from_candles(c, date(2026, 11, 12)))
        self.assertIsNone(r.prev_close_from_candles([], date(2026, 11, 13)))

    def test_bad_price_values_ignored(self):
        def call(method, path, **kw):
            if path == "/oauth2/token":
                return {"access_token": "jwt"}
            if path == "/api/v1/prices":
                return {"result": [{"symbol": "000660", "lastPrice": "-1"},
                                   {"symbol": "005930", "lastPrice": "<script>"},
                                   {"symbol": "035720", "lastPrice": "100", "currency": "USD"}]}
            return {"result": {"candles": []}}
        feed = r.TossPrices(call=call, load_creds=keychain, log=lambda *a: None)
        feed.want(["000660", "005930", "035720"])
        feed.poll_once()
        self.assertEqual(feed.snapshot(["000660", "005930", "035720"])["prices"], [])

    def test_missing_keys(self):
        feed = r.TossPrices(call=FakeToss(), load_creds=lambda: None, log=lambda *a: None)
        with self.assertRaises(r.RelayError) as cm:
            feed.ensure_token()
        self.assertIn("setup", str(cm.exception))


class OpenAppTest(unittest.TestCase):
    def run_with(self, ok):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0 if ok(cmd) else 1)
        with mock.patch.object(r.subprocess, "run", fake_run), mock.patch("builtins.print"):
            return r.open_app(), calls

    def test_opens_chrome_app_window(self):
        got, calls = self.run_with(lambda cmd: True)
        self.assertEqual(calls, [["/usr/bin/open", "-na", "Google Chrome", "--args", f"--app={r.APP_URL}"]])
        self.assertTrue(all("Google Chrome" in c for c in calls))  # 이름이 같은 사파리 웹앱을 열지 않아요

    def test_falls_back_to_chrome_tab(self):
        got, calls = self.run_with(lambda cmd: "--args" not in cmd)
        self.assertEqual(got, ["/usr/bin/open", "-a", "Google Chrome", r.APP_URL])

    def test_nothing_found(self):
        got, _ = self.run_with(lambda cmd: False)
        self.assertIsNone(got)


class TokenFileTest(unittest.TestCase):
    def test_token_file_private_and_stable(self):
        with tempfile.TemporaryDirectory() as d:
            os.chmod(d, 0o700)
            path = Path(d) / ".stockmap" / "relay-token"
            with mock.patch.object(r, "TOKEN_FILE", path):
                t1, created = r.load_or_create_token()
                t2, created2 = r.load_or_create_token()
                t3, _ = r.load_or_create_token(force_new=True)
            self.assertTrue(created and not created2)
            self.assertEqual(t1, t2)
            self.assertNotEqual(t1, t3)
            self.assertRegex(t1, r"^[0-9a-f]{40}$")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main(verbosity=2)
