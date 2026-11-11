#!/usr/bin/env python3
"""
스톡맵 맥 동기화 — 토스증권 보유 종목을 스톡맵 '가져오기' 파일로 저장해요.

안전 장치
  1. 조회 전용: 토큰 발급, 계좌 목록, 보유 종목 3가지 호출만 허용해요.
     나머지(주문 등)는 코드 안에서 차단되고, 주문 기능 코드는 아예 없어요.
  2. client_secret은 macOS 키체인에만 저장해요. 파일·명령어 기록에 남지 않아요.
  3. 파이썬 기본 기능만 써요 (외부 패키지 설치 없음).
  4. 계좌번호는 화면에도, 결과 파일에도 쓰지 않아요.
  5. 다른 주소로 넘기는 리다이렉트는 따라가지 않아요 (토큰 유출 방지).

사용법
  python3 stockmap_sync.py setup    # 처음 한 번: 키를 키체인에 저장
  python3 stockmap_sync.py          # 보유 종목 받기 → 다운로드 폴더에 저장
  python3 stockmap_sync.py forget   # 키체인에서 키 삭제
"""
import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE = "https://openapi.tossinvest.com"
KEYCHAIN_SERVICE = "stockmap-tossinvest"
SECURITY_BIN = "/usr/bin/security"
# 이 프로그램이 부를 수 있는 호출은 이 세 개뿐이에요.
ALLOWED = {
    ("POST", "/oauth2/token"),
    ("GET", "/api/v1/accounts"),
    ("GET", "/api/v1/holdings"),
}
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
MAX_RESPONSE = 2_000_000
MAX_HOLDINGS = 50
OUT_NAME = "stockmap-holdings.json"
GROUPS_FILE = Path(__file__).resolve().parent / "groups.json"


class SyncError(Exception):
    pass


# ---------------------------------------------------------------------------
# 키체인
# ---------------------------------------------------------------------------
def keychain_get(account):
    r = subprocess.run(
        [SECURITY_BIN, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    value = r.stdout.rstrip("\n")
    return value or None


def keychain_set_interactive(account, label):
    print(f"\n{label}을(를) 붙여넣고 Enter를 누르세요. 화면에는 보이지 않아요. (확인을 위해 두 번 물어봐요)")
    # -w를 마지막에 두면 security가 직접 입력을 받아요. 비밀값이 이 프로그램이나 명령어 기록을 거치지 않아요.
    r = subprocess.run([SECURITY_BIN, "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                        "-a", account, "-l", f"StockMap 토스증권 {label}", "-w"])
    if r.returncode != 0:
        raise SyncError("키체인에 저장하지 못했어요.")


def keychain_delete(account):
    subprocess.run([SECURITY_BIN, "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
                   capture_output=True)


# ---------------------------------------------------------------------------
# HTTP (허용된 호출만, 리다이렉트 금지)
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError("서버가 다른 주소로 보내려 해서 중단했어요 (보안).")


OPENER = urllib.request.build_opener(
    _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
)

ERROR_HELP = {
    "ip-not-allowed": "지금 인터넷의 IP가 토스증권 '허용 IP'에 없어요. 토스증권 웹(WTS) → 설정 → Open API → 허용 IP 관리에서 지금 IP를 추가하세요.",
    "access_denied": "토큰 발급이 거부됐어요. 허용 IP나 client_id·client_secret을 확인하세요.",
    "invalid_client": "client_id 또는 client_secret이 맞지 않아요. 'setup'으로 다시 저장하세요.",
    "expired-token": "토큰이 만료됐어요. 다시 실행해 주세요.",
    "token-revoked": "다른 곳에서 새 토큰을 발급해 이 토큰이 무효가 됐어요. 다시 실행해 주세요.",
}


def _error_code(body):
    try:
        j = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    err = j.get("error") if isinstance(j, dict) else None
    if isinstance(err, str):
        return err
    if isinstance(err, dict) and isinstance(err.get("code"), str):
        return err["code"]
    return None


def call(method, path, *, token=None, account_seq=None, form=None, query=None):
    if (method, path) not in ALLOWED:
        raise SyncError(f"허용되지 않은 호출이라 막았어요: {method} {path}")
    url = BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"Accept": "application/json", "User-Agent": "stockmap-sync/1.0"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    if account_seq is not None:
        headers["X-Tossinvest-Account"] = str(account_seq)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    for attempt in range(3):
        try:
            with OPENER.open(req, timeout=20) as res:
                body = res.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise SyncError("응답이 비정상적으로 커서 중단했어요.")
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read(20000)
            if e.code == 429 and attempt < 2:
                try:
                    wait = min(10.0, float(e.headers.get("Retry-After", "1")))
                except ValueError:
                    wait = 1.0
                time.sleep(wait + 0.5 * attempt)
                continue
            code = _error_code(body)
            hint = ERROR_HELP.get(code or "", "")
            raise SyncError(f"토스증권 응답 오류 {e.code} ({code or '코드 없음'}). {hint}".strip())
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e))
            if "CERTIFICATE" in reason.upper():
                raise SyncError("인증서 확인에 실패했어요. python.org에서 받은 파이썬이라면 "
                                "'Install Certificates.command'를 한 번 실행하세요.")
            raise SyncError(f"인터넷 연결 오류: {reason}")
        except ValueError:
            raise SyncError("응답 형식이 올바르지 않아요.")
    raise SyncError("요청이 너무 많아 잠시 후 다시 시도해 주세요.")


# ---------------------------------------------------------------------------
# 데이터 변환
# ---------------------------------------------------------------------------
def dec(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def load_groups():
    """groups.json (선택): {"VOO": "미국 지수", "000660": "반도체"} 형태"""
    if not GROUPS_FILE.exists():
        return {}
    try:
        data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    except ValueError:
        print("! groups.json 형식이 잘못돼서 무시했어요.")
        return {}
    out = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str) and SYMBOL_RE.match(k.upper()):
                out[k.upper()] = re.sub(r"[\x00-\x1f\x7f]", "", v).strip()[:20]
    return out


def build_holdings(items, groups):
    merged = {}
    skipped = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("symbol", "")).strip().upper()
        cur = it.get("currency")
        qty, avg, last = dec(it.get("quantity")), dec(it.get("averagePurchasePrice")), dec(it.get("lastPrice"))
        if not SYMBOL_RE.match(sym) or cur not in ("USD", "KRW") or qty is None or qty <= 0:
            skipped.append(sym or "?")
            continue
        avg = avg if avg is not None and avg >= 0 else Decimal(0)
        m = merged.get(sym)
        if m:  # 여러 계좌에 같은 종목 → 수량 합치고 평단은 가중 평균
            total = m["qty"] + qty
            m["avg"] = (m["avg"] * m["qty"] + avg * qty) / total
            m["qty"] = total
            if last is not None and last > 0:
                m["last"] = last
        else:
            country = it.get("marketCountry")
            default_group = "미국" if country == "US" else ("한국" if country == "KR" else "기타")
            merged[sym] = {"qty": qty, "avg": avg, "last": last if last and last > 0 else None,
                           "cur": cur, "group": groups.get(sym) or default_group}
    out = []
    for sym, m in merged.items():
        out.append({
            "symbol": sym,
            "currency": m["cur"],
            "shares": float(m["qty"]),
            "avgPrice": round(float(m["avg"]), 4),
            "group": m["group"],
            # 토스증권 현재가를 '직접 입력 가격'으로 넣어 둬요. 한국 종목은 이 값으로 표시되고,
            # 미국 종목은 Finnhub 실시간 가격이 있으면 그걸 우선 써요.
            "manualPrice": round(float(m["last"]), 4) if m["last"] is not None else None,
        })
    return out, skipped


def write_output(holdings, out_path):
    payload = json.dumps({"version": 1, "source": "tossinvest", "holdings": holdings},
                         ensure_ascii=False, indent=2)
    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 나만 읽을 수 있는 권한(600)으로 저장
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    os.chmod(out_path, 0o600)
    return out_path


# ---------------------------------------------------------------------------
# 명령
# ---------------------------------------------------------------------------
def cmd_setup():
    print("토스증권 웹(WTS) → 설정 → Open API에서 client_id와 client_secret을 복사해 두세요.")
    keychain_set_interactive("client_id", "client_id")
    keychain_set_interactive("client_secret", "client_secret")
    print("\n키체인에 저장했어요. 이제 'python3 stockmap_sync.py'로 실행하세요.")


def cmd_forget():
    keychain_delete("client_id")
    keychain_delete("client_secret")
    print("키체인에서 토스증권 키를 지웠어요.")


def cmd_sync(out_path):
    client_id = keychain_get("client_id")
    client_secret = keychain_get("client_secret")
    if not client_id or not client_secret:
        raise SyncError("저장된 키가 없어요. 먼저 'python3 stockmap_sync.py setup'을 실행하세요.")

    print("1/3 토큰 받는 중…")
    tok = call("POST", "/oauth2/token", form={
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    client_secret = None  # 더 이상 필요 없음
    token = tok.get("access_token") if isinstance(tok, dict) else None
    if not isinstance(token, str) or not token or len(token) > 8192 or any(c.isspace() for c in token):
        raise SyncError("토큰 응답이 올바르지 않아요.")

    print("2/3 계좌 목록 받는 중…")
    acc = call("GET", "/api/v1/accounts", token=token)
    accounts = acc.get("result") if isinstance(acc, dict) else None
    if not isinstance(accounts, list) or not accounts:
        raise SyncError("조회할 수 있는 계좌가 없어요.")

    print("3/3 보유 종목 받는 중…")
    items = []
    for a in accounts:
        if not isinstance(a, dict):
            continue
        try:
            seq = int(a.get("accountSeq"))
        except (TypeError, ValueError):
            continue
        time.sleep(1.1)  # ACCOUNT·ASSET 호출 한도 여유
        h = call("GET", "/api/v1/holdings", token=token, account_seq=seq)
        result = h.get("result") if isinstance(h, dict) else None
        got = result.get("items") if isinstance(result, dict) else None
        if isinstance(got, list):
            items.extend(got)
            print(f"   · {a.get('accountType', '계좌')}: {len(got)}종목")  # 계좌번호는 출력하지 않아요
    token = None

    holdings, skipped = build_holdings(items, load_groups())
    if skipped:
        print(f"! 형식이 맞지 않아 뺀 종목: {', '.join(skipped[:10])}")
    if len(holdings) > MAX_HOLDINGS:
        print(f"! 종목이 {len(holdings)}개라 앞의 {MAX_HOLDINGS}개만 저장해요 (스톡맵 한도).")
        holdings = holdings[:MAX_HOLDINGS]
    if not holdings:
        raise SyncError("보유 종목이 없어요.")
    path = write_output(holdings, out_path)
    print(f"\n완료! {len(holdings)}종목을 저장했어요 → {path}")
    print("스톡맵 → 종목 관리 → 가져오기(JSON)에서 이 파일을 선택하세요. 가져온 뒤 파일은 지워도 돼요.")


def main(argv=None):
    if sys.platform != "darwin" and os.environ.get("STOCKMAP_SYNC_TEST") != "1":
        print("이 프로그램은 macOS 전용이에요 (키체인 사용).")
        return 2
    p = argparse.ArgumentParser(description="토스증권 보유 종목 → 스톡맵 가져오기 파일 (조회 전용)")
    p.add_argument("command", nargs="?", default="sync", choices=["sync", "setup", "forget"])
    p.add_argument("--out", default=str(Path.home() / "Downloads" / OUT_NAME), help="저장 위치")
    args = p.parse_args(argv)
    try:
        if args.command == "setup":
            cmd_setup()
        elif args.command == "forget":
            cmd_forget()
        else:
            cmd_sync(args.out)
    except SyncError as e:
        print(f"\n중단: {e}")
        return 1
    except KeyboardInterrupt:
        print("\n취소했어요.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
