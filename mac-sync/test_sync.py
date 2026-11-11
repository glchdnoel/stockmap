"""stockmap_sync 모의 점검 (실제 토스증권·키체인에 접속하지 않음). 실행: python3 test_sync.py"""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ["STOCKMAP_SYNC_TEST"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stockmap_sync as s  # noqa: E402


class FakeRes(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_server(calls):
    def open_(req, timeout=None):
        path = req.full_url.split("openapi.tossinvest.com", 1)[1].split("?")[0]
        calls.append((req.get_method(), path, dict(req.header_items())))
        if path == "/oauth2/token":
            return FakeRes(json.dumps({"access_token": "jwt.token.x", "token_type": "Bearer", "expires_in": 3600}).encode())
        if path == "/api/v1/accounts":
            return FakeRes(json.dumps({"result": [
                {"accountNo": "123-45-678901", "accountSeq": 1, "accountType": "BROKERAGE"},
                {"accountNo": "999-99-999999", "accountSeq": 2, "accountType": "PENSION_SAVINGS"}]}).encode())
        if path == "/api/v1/holdings":
            seq = dict(req.header_items()).get("X-tossinvest-account")
            items = [
                {"symbol": "VOO", "name": "Vanguard", "marketCountry": "US", "currency": "USD",
                 "quantity": "2", "averagePurchasePrice": "500", "lastPrice": "520.5"},
                {"symbol": "000660", "name": "SK하이닉스", "marketCountry": "KR", "currency": "KRW",
                 "quantity": 3, "averagePurchasePrice": 180000, "lastPrice": 205000},
                {"symbol": "<script>", "currency": "USD", "quantity": 1, "averagePurchasePrice": 1},
                {"symbol": "EURX", "currency": "EUR", "quantity": 1, "averagePurchasePrice": 1},
            ] if seq == "1" else [
                {"symbol": "VOO", "marketCountry": "US", "currency": "USD",
                 "quantity": "1", "averagePurchasePrice": "530", "lastPrice": "520.5"},
            ]
            return FakeRes(json.dumps({"result": {"items": items}}).encode())
        raise AssertionError("unexpected path " + path)
    return open_


def fake_keychain(account):
    return {"client_id": "tsck_live_test", "client_secret": "secret-xyz"}.get(account)


class SyncTest(unittest.TestCase):
    def run_sync(self, out=None):
        calls = []
        tmp = Path(out) if out else Path(tempfile.mkdtemp()) / "out.json"
        with mock.patch.object(s, "keychain_get", fake_keychain), \
             mock.patch.object(s.OPENER, "open", fake_server(calls)), \
             mock.patch.object(s.time, "sleep", lambda x: None), \
             mock.patch.object(s, "GROUPS_FILE", Path("/nonexistent")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = s.main(["--out", str(tmp)])
        return code, calls, tmp, out.getvalue()

    def test_end_to_end(self):
        code, calls, tmp, printed = self.run_sync()
        self.assertEqual(code, 0, printed)
        data = json.loads(tmp.read_text())
        by = {h["symbol"]: h for h in data["holdings"]}
        self.assertEqual(set(by), {"VOO", "000660"})           # 위험·미지원 종목 제외
        self.assertEqual(by["VOO"]["shares"], 3.0)              # 두 계좌 합산
        self.assertAlmostEqual(by["VOO"]["avgPrice"], 510.0)    # (2*500+1*530)/3
        self.assertEqual(by["000660"]["manualPrice"], 205000.0)
        self.assertEqual(by["000660"]["group"], "한국")
        self.assertEqual(stat.S_IMODE(os.stat(tmp).st_mode), 0o600)  # 나만 읽기
        text = tmp.read_text() + printed
        for secret in ("123-45-678901", "999-99-999999", "secret-xyz", "jwt.token.x"):
            self.assertNotIn(secret, text)                      # 계좌번호·키·토큰 미노출

    def test_symlink_target_untouched(self):
        d = Path(tempfile.mkdtemp())
        victim = d / "victim.txt"
        victim.write_text("original")
        os.chmod(victim, 0o644)
        out = d / "out.json"
        os.symlink(victim, out)                                  # 결과 경로에 링크를 미리 만들어 둠
        code, _, _, printed = self.run_sync(out)
        self.assertEqual(code, 0, printed)
        self.assertEqual(victim.read_text(), "original")         # 링크 대상 내용 그대로
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o644)  # 링크 대상 권한 그대로
        self.assertFalse(out.is_symlink())                       # 링크는 일반 파일로 교체됨
        self.assertEqual(stat.S_IMODE(os.lstat(out).st_mode), 0o600)
        self.assertIn("holdings", json.loads(out.read_text()))
        self.assertEqual([p.name for p in d.iterdir() if p.suffix == ".tmp"], [])  # 임시 파일 없음

    def test_refuses_writable_dir(self):
        d = Path(tempfile.mkdtemp())
        os.chmod(d, 0o777)                                       # 다른 사용자도 쓸 수 있는 폴더
        try:
            code, _, _, printed = self.run_sync(d / "out.json")
        finally:
            os.chmod(d, 0o700)
        self.assertEqual(code, 1, printed)
        self.assertIn("다른 사용자", printed)
        self.assertFalse((d / "out.json").exists())

    def test_only_allowed_calls(self):
        _, calls, _, _ = self.run_sync()
        self.assertTrue(all((m, p) in s.ALLOWED for m, p, _ in calls))
        self.assertEqual([c[:2] for c in calls][0], ("POST", "/oauth2/token"))

    def test_blocks_other_endpoints(self):
        with self.assertRaises(s.SyncError):
            s.call("POST", "/api/v1/orders", token="t")
        with self.assertRaises(s.SyncError):
            s.call("GET", "/api/v1/orders", token="t")

    def test_redirect_refused(self):
        h = s._NoRedirect()
        with self.assertRaises(s.SyncError):
            h.redirect_request(None, None, 302, "Found", {}, "https://evil.example/")

    def test_ip_not_allowed_message(self):
        def open_(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b'{"error":{"code":"ip-not-allowed"}}'))
        with mock.patch.object(s.OPENER, "open", open_):
            with self.assertRaises(s.SyncError) as cm:
                s.call("GET", "/api/v1/accounts", token="t")
        self.assertIn("허용 IP", str(cm.exception))

    def test_missing_keys(self):
        with mock.patch.object(s, "keychain_get", lambda a: None), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = s.main(["--out", "/tmp/x.json"])
        self.assertEqual(code, 1)
        self.assertIn("setup", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
