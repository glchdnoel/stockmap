"""스톡맵 자동 점검: CSP 위반, XSS 시도, 입력 검증, 실시간(모의) 동작.
실행: 저장소 최상위 폴더에서 `python3 tests/test_app.py` (포트 8765에 테스트용 서버를 직접 띄워요)."""
import http.server, json, re, sys, threading
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
BASE = f"http://localhost:{PORT}/index.html"
PROD_SCRIPT_SRC = "https://glchdnoel.github.io/stockmap/"
CSP_RE = re.compile(r'<meta http-equiv="Content-Security-Policy" content="[^"]*">')


def local_index_html():
    """실제 index.html은 그대로 두고, 보낼 때 CSP의 운영 주소 부분만 로컬 주소로 바꿔요."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    m = CSP_RE.search(html)
    if not m or PROD_SCRIPT_SRC not in m.group(0):
        raise SystemExit("index.html에서 CSP 또는 운영 script-src 주소를 찾지 못했어요.")
    csp = m.group(0).replace(PROD_SCRIPT_SRC, f"http://localhost:{PORT}/")
    return html[:m.start()] + csp + html[m.end():]


class TestServer(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            body = local_index_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


server = http.server.ThreadingHTTPServer(("localhost", PORT), TestServer)
threading.Thread(target=server.serve_forever, daemon=True).start()
results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, detail)

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 720})
    page = ctx.new_page()
    console, dialogs, external = [], [], []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.on("request", lambda r: external.append(r.url) if not r.url.startswith("http://localhost") else None)

    # 1) 데모 화면
    page.goto(BASE); page.wait_for_timeout(2500)
    page.screenshot(path="./demo.png")
    leaves = page.locator(".leaf").count()
    check("데모 박스 렌더링", leaves == 9, f"{leaves}개")
    check("CSP 위반 없음(데모)", not any("Content Security Policy" in c for c in console), str(console))
    check("외부 요청 없음(데모)", not external, str(external))

    # 2) XSS 시도: 그룹명에 HTML/스크립트
    page.click("#manageBtn")
    payload = '<img src=x onerror=alert(1)>'
    page.fill("#fSymbol", "VOO"); page.fill("#fShares", "1"); page.fill("#fAvg", "100")
    page.fill("#fGroup", payload[:20])
    page.click("#addForm button[type=submit]")
    page.wait_for_timeout(300)
    page.fill("#fSymbol", "<script>"); page.fill("#fShares", "1"); page.fill("#fAvg", "1")
    page.click("#addForm button[type=submit]")
    msg = page.text_content("#formMsg")
    check("위험한 티커 거부", "티커" in (msg or ""), msg)
    page.fill("#fSymbol", "QQQ"); page.fill("#fShares", "-5"); page.fill("#fAvg", "1")
    page.click("#addForm button[type=submit]")
    check("음수 수량 거부", "범위" in (page.text_content("#formMsg") or ""), page.text_content("#formMsg"))
    page.click("[data-close=manageDlg]")
    page.wait_for_timeout(500)
    check("img 태그 미생성", page.locator("#map img").count() == 0)
    check("그룹명이 글자로 표시", payload[:20] in page.inner_text("#map"))
    check("alert 실행 안 됨", not dialogs, str(dialogs))

    # 3) 조작된 localStorage 주입
    evil = {"settings": {"fxRate": "1e999", "range": "<x>"}, "holdings": [
        {"symbol": "AAPL\"><svg onload=alert(2)>", "currency": "USD", "shares": 1, "avgPrice": 1},
        {"symbol": "MSFT", "currency": "EUR", "shares": 1, "avgPrice": 1},
        {"symbol": "NVDA", "currency": "USD", "shares": 1, "avgPrice": 1, "group": "‮evil"},
    ]}
    page.evaluate("d => localStorage.setItem('stockmap.v1', JSON.stringify(d))", evil)
    page.evaluate("() => localStorage.setItem('stockmap.apikey', 'bad key <x>')")
    page.reload(); page.wait_for_timeout(800)
    syms = page.eval_on_selector_all(".leaf", "els => els.map(e => e.dataset.symbol)")
    check("잘못된 저장 데이터 걸러냄", syms == ["NVDA"], str(syms))
    check("잘못된 키는 무시", "API 키 없음" in page.text_content("#conn"), page.text_content("#conn"))
    check("alert 실행 안 됨(저장소)", not dialogs, str(dialogs))

    # 4) 가짜 가져오기 파일
    page.evaluate("() => localStorage.clear()"); page.reload(); page.wait_for_timeout(500)
    page.click("#manageBtn")
    bad = json.dumps({"holdings": [{"symbol": "../../etc", "currency": "USD", "shares": 1, "avgPrice": 1}]})
    page.set_input_files("#importFile", files=[{"name": "x.json", "mimeType": "application/json", "buffer": bad.encode()}])
    page.wait_for_timeout(300)
    check("위험한 가져오기 파일 거부", "실패" in (page.text_content("#formMsg") or ""), page.text_content("#formMsg"))
    page.click("[data-close=manageDlg]")
    ctx.close()

    # 5) 실시간 모의: Finnhub REST/웹소켓을 가짜 서버로 대체
    ctx = b.new_context(viewport={"width": 1280, "height": 720})
    page = ctx.new_page()
    console.clear()
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    sent_hosts = []
    page.on("request", lambda r: sent_hosts.append(r.url.split('/')[2]))
    outbound = []  # 시세 업체로 나가는 REST 요청 (주소, 본문)
    page.on("request", lambda r: outbound.append((r.url, r.post_data)) if r.url.startswith(("https://finnhub.io", "https://api.twelvedata.com")) else None)
    prices = {"VOO": (500, 495), "QQQ": (470, 480), "SCHD": (27, 27.1), "SOXX": (230, 220)}
    def quote(route):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(route.request.url).query)
        s = q["symbol"][0]
        c, pc = prices.get(s, (0, 0))
        route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
                      body=json.dumps({"c": c, "pc": pc, "d": c - pc, "dp": 0}))
    page.route("https://finnhub.io/api/v1/quote*", quote)
    ws_state = {"subs": [], "urls": [], "msgs": []}
    def on_ws(ws):
        ws_state["urls"].append(ws.url)
        def on_msg(m):
            ws_state["msgs"].append(m)
            ws_state["subs"].append(json.loads(m)["symbol"])
            if len(ws_state["subs"]) == 3:
                ws.send(json.dumps({"type": "trade", "data": [
                    {"s": "VOO", "p": 510, "t": 0, "v": 1},
                    {"s": "EVIL", "p": 1, "t": 0, "v": 1},
                    {"s": "QQQ", "p": "<b>x</b>", "t": 0, "v": 1}]}))
        ws.on_message(on_msg)
    page.route_web_socket("wss://ws.finnhub.io/**", on_ws)
    page.goto(BASE)
    holdings = [
        {"symbol": "VOO", "currency": "USD", "shares": 6, "avgPrice": 480, "group": "미국 지수"},
        {"symbol": "QQQ", "currency": "USD", "shares": 4, "avgPrice": 450, "group": "미국 지수"},
        {"symbol": "SOXX", "currency": "USD", "shares": 5, "avgPrice": 210, "group": "반도체"},
        {"symbol": "000660", "currency": "KRW", "shares": 3, "avgPrice": 180000, "group": "한국", "manualPrice": 205000},
    ]
    page.evaluate("h => { localStorage.setItem('stockmap.v1', JSON.stringify({settings: {rememberHoldings: true}, holdings: h})); localStorage.setItem('stockmap.apikey', 'abcdEFGH1234test'); }", holdings)
    page.reload(); page.wait_for_timeout(4000)
    check("웹소켓 구독(USD 3종목)", sorted(ws_state["subs"]) == ["QQQ", "SOXX", "VOO"], str(ws_state["subs"]))
    check("실시간 상태 표시", page.text_content("#conn") == "실시간", page.text_content("#conn"))
    voo = page.text_content(".leaf[data-symbol=VOO] .pct")
    check("체결가 반영 (510 vs 495 → +3.03%)", voo == "+3.03%", voo)
    qqq = page.text_content(".leaf[data-symbol=QQQ] .pct")
    check("잘못된 체결 데이터 무시", qqq == "-2.08%", qqq)
    check("통신 대상이 허용 목록뿐", set(sent_hosts) <= {"localhost:8765", "finnhub.io", "ws.finnhub.io"}, str(set(sent_hosts)))
    check("CSP 위반 없음(실시간)", not any("Content Security Policy" in c for c in console), str(console))
    page.screenshot(path="./live.png")
    page.hover(".leaf[data-symbol=VOO]"); page.wait_for_timeout(200)
    page.screenshot(path="./tooltip.png")

    # 6) 기간 선택: Twelve Data 모의 응답 (새로고침 후 현재가는 REST 값 500)
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    today = datetime.now(ny).date()
    days = [today - timedelta(days=i) for i in range(1, 1300) if (today - timedelta(days=i)).weekday() < 5][:800]
    td_calls = []
    def daily_close(sym, d):  # 날짜마다 다른 가격
        return {"VOO": 400, "QQQ": 500, "SOXX": 200}[sym] + (today - d).days
    def td(route):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(route.request.url).query)
        sym, interval = q["symbol"][0], q["interval"][0]
        td_calls.append((sym, interval, q.get("apikey", [""])[0]))
        if interval == "1day":
            dd = days[:100] if sym == "SOXX" else days  # SOXX는 상장 100일짜리로 가정
            vals = [{"datetime": d.isoformat(), "close": str(daily_close(sym, d))} for d in dd]
            vals.insert(0, {"datetime": "<img src=x onerror=alert(9)>", "close": "1"})
        else:
            base = datetime(2026, 11, 10, 15, 55)
            vals = [{"datetime": (base - timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S"), "close": str(100 + i)} for i in range(24)]
        route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
                      body=json.dumps({"status": "ok", "values": vals}))
    page.route("https://api.twelvedata.com/**", td)
    page.click(".period-btn[data-period='1w']"); page.wait_for_timeout(500)
    check("키 없으면 안내 표시", "Twelve Data" in page.text_content("#banner"), page.text_content("#banner"))
    page.evaluate("() => localStorage.setItem('stockmap.tdkey', 'tdkeyABC123456')")
    page.reload(); page.wait_for_timeout(20000)
    target = (datetime.now(ny) - timedelta(days=7)).date()
    base_d = next(d for d in days if d <= target)
    exp = (500 - daily_close("VOO", base_d)) / daily_close("VOO", base_d) * 100
    got = page.text_content(".leaf[data-symbol=VOO] .pct")
    check("1주 등락 계산", got == f"{exp:+.2f}%", f"{got} vs {exp:+.2f}%")
    check("Twelve Data 요청 간격(한도 준수)", len([c for c in td_calls if c[1] == "1day"]) == 3, str(td_calls))
    check("기간 라벨 표시", page.text_content("#periodLabel") == "1주")
    page.click(".period-btn[data-period='1m']"); page.wait_for_timeout(800)
    target = (datetime.now(ny) - timedelta(days=30)).date()
    base_d = next(d for d in days if d <= target)
    exp = (500 - daily_close("VOO", base_d)) / daily_close("VOO", base_d) * 100
    got = page.text_content(".leaf[data-symbol=VOO] .pct")
    check("1달 등락 (캐시 사용, 추가 요청 없음)", got == f"{exp:+.2f}%" and len(td_calls) == 3, f"{got} vs {exp:+.2f}%, calls={len(td_calls)}")
    def exp_for(days_back):
        t = (datetime.now(ny) - timedelta(days=days_back)).date()
        bd = next(d for d in days if d <= t)
        return f"{(500 - daily_close('VOO', bd)) / daily_close('VOO', bd) * 100:+.2f}%"
    for per, n in [("3m", 91), ("6m", 182), ("1y", 365), ("3y", 1095)]:
        page.click(f".period-btn[data-period='{per}']"); page.wait_for_timeout(400)
        got = page.text_content(".leaf[data-symbol=VOO] .pct")
        check(f"{per} 등락 계산(추가 요청 없음)", got == exp_for(n) and len(td_calls) == 3, f"{got} vs {exp_for(n)}, calls={len(td_calls)}")
    check("짧은 이력 종목은 '기간 부족'", page.text_content(".leaf[data-symbol=SOXX] .pct") == "기간 부족", page.text_content(".leaf[data-symbol=SOXX] .pct"))
    page.hover(".leaf[data-symbol=VOO]"); page.wait_for_timeout(200)
    check("장기 기준가에 배당 제외 표시", "배당 제외" in page.inner_text("#tooltip"), page.inner_text("#tooltip"))
    page.mouse.move(5, 5)
    page.click(".period-btn[data-period='1h']"); page.wait_for_timeout(18000)
    exp = (500 - 112) / 112 * 100  # 새로고침 후 현재가 500(REST), 15:55 기준 1시간 전 = 14:55 → close 100+12
    got = page.text_content(".leaf[data-symbol=VOO] .pct")
    check("1시간 등락 계산", got == f"{exp:+.2f}%", f"{got} vs {exp:+.2f}%")
    page.hover(".leaf[data-symbol=VOO]"); page.wait_for_timeout(200)
    page.screenshot(path="./period.png")
    check("통신 대상이 허용 목록뿐(기간)", set(sent_hosts) <= {"localhost:8765", "finnhub.io", "ws.finnhub.io", "api.twelvedata.com"}, str(set(sent_hosts)))
    check("CSP 위반 없음(기간)", not any("Content Security Policy" in c for c in console), str(console))
    check("alert 실행 안 됨(기간)", not dialogs, str(dialogs))

    # 나가는 요청 점검: 종목 코드와 키 외에 수량·평단·평가액이 실리지 않는지
    from urllib.parse import urlparse, parse_qs
    usd_syms = {"VOO", "QQQ", "SOXX"}
    allowed_params = {
        "finnhub.io": {"symbol": usd_syms, "token": None},
        "api.twelvedata.com": {"symbol": usd_syms, "interval": {"1day", "5min"}, "outputsize": {"800", "24"},
                               "timezone": {"America/New_York"}, "apikey": None},
    }
    leaks = []
    for url, body in outbound:
        u = urlparse(url)
        rule = allowed_params.get(u.hostname, {})
        if body:
            leaks.append(("본문", url))
        for k, vs in parse_qs(u.query).items():
            if k not in rule or (rule[k] is not None and not set(vs) <= rule[k]):
                leaks.append(("항목", k, vs))
    for url in ws_state["urls"]:
        if set(parse_qs(urlparse(url).query)) != {"token"}:
            leaks.append(("웹소켓 주소", url))
    for m in ws_state["msgs"]:
        msg = json.loads(m)
        if set(msg) != {"type", "symbol"} or msg["symbol"] not in usd_syms:
            leaks.append(("웹소켓 메시지", m))
    check("나가는 요청을 실제로 잡음", len(outbound) > 0 and len(ws_state["msgs"]) > 0, f"REST {len(outbound)}, WS {len(ws_state['msgs'])}")
    check("시세 요청에 종목 코드·키·조회 옵션만 들어감(수량·평단·평가액 없음)", not leaks, str(leaks))
    page.click(".period-btn[data-period='1w']"); page.wait_for_timeout(500)
    page.mouse.move(5, 5); page.wait_for_timeout(300)
    page.screenshot(path="./week.png")
    check("기억하기 켜면 과거 가격 캐시 저장", page.evaluate("() => localStorage.getItem('stockmap.hist.v1') !== null"))
    page.click("#settingsBtn"); page.uncheck("#fRememberHoldings"); page.click("#saveSettingsBtn"); page.wait_for_timeout(300)
    st = page.evaluate("() => [localStorage.getItem('stockmap.hist.v1'), 'holdings' in JSON.parse(localStorage.getItem('stockmap.v1') || '{}')]")
    check("기억하기 끄면 과거 가격 캐시도 삭제", st == [None, False], str(st))
    page.click(".period-btn[data-period='1m']"); page.wait_for_timeout(2000)
    check("끄고 나서도 이번 탭에서는 캐시 사용(저장 안 함)", page.text_content(".leaf[data-symbol=VOO] .pct").endswith("%")
          and page.evaluate("() => localStorage.getItem('stockmap.hist.v1')") is None, page.text_content(".leaf[data-symbol=VOO] .pct"))
    page.click("[data-close=settingsDlg]")

    # 7) 보안 설정: 키 기억 끄기, 전체 삭제
    page.on("dialog", lambda d: d.accept())
    page.click("#settingsBtn")
    check("설정 화면에 키 일부도 표시 안 함", page.text_content("#keyStatus") == "저장됨" and page.text_content("#tdKeyStatus") == "저장됨",
          f"{page.text_content('#keyStatus')} / {page.text_content('#tdKeyStatus')}")
    page.uncheck("#fRemember"); page.click("#saveSettingsBtn"); page.wait_for_timeout(300)
    st = page.evaluate("() => [localStorage.getItem('stockmap.apikey'), sessionStorage.getItem('stockmap.apikey'), localStorage.getItem('stockmap.tdkey'), sessionStorage.getItem('stockmap.tdkey')]")
    check("기억 끄면 키가 세션에만", st[0] is None and st[2] is None and st[1] and st[3], str(st))
    page.click("#wipeBtn"); page.wait_for_timeout(500)
    st = page.evaluate("() => [localStorage.length, sessionStorage.length]")
    check("모든 데이터 지우기", st == [0, 0], str(st))
    check("금액 숨기기 버튼 글자", page.text_content("#maskBtn") == "금액 숨기기", page.text_content("#maskBtn"))
    ctx.close()

    # 8) 보유 종목 기억하기 스위치 (기본 꺼짐)
    ctx = b.new_context(viewport={"width": 1280, "height": 720})
    page = ctx.new_page()
    page.on("dialog", lambda d: d.accept())
    stored = lambda: json.loads(page.evaluate("() => localStorage.getItem('stockmap.v1') || '{}'"))
    page.goto(BASE); page.wait_for_timeout(800)
    page.click("#manageBtn")
    page.fill("#fSymbol", "VOO"); page.fill("#fShares", "2"); page.fill("#fAvg", "400")
    page.click("#addForm button[type=submit]"); page.wait_for_timeout(300)
    page.click("[data-close=manageDlg]")
    check("기본값: 보유 종목을 저장하지 않음", "holdings" not in stored(), str(stored()))
    page.click("#settingsBtn")
    check("기억하기 기본 꺼짐", not page.is_checked("#fRememberHoldings"))
    page.check("#fRememberHoldings"); page.click("#saveSettingsBtn"); page.wait_for_timeout(300)
    check("켜면 보유 종목 저장", [h["symbol"] for h in stored().get("holdings", [])] == ["VOO"], str(stored()))
    page.uncheck("#fRememberHoldings"); page.click("#saveSettingsBtn"); page.wait_for_timeout(300)
    check("끄면 저장된 보유 종목 삭제", "holdings" not in stored(), str(stored()))
    check("끈 뒤에도 이번 탭에서는 표시", page.locator(".leaf[data-symbol=VOO]").count() == 1)
    page.evaluate("() => localStorage.setItem('stockmap.v1', JSON.stringify({holdings: [{symbol: 'QQQ', currency: 'USD', shares: 1, avgPrice: 1}]}))")
    page.reload(); page.wait_for_timeout(800)
    check("예전 저장값은 불러온 뒤 저장소에서 지움", "holdings" not in stored() and page.locator(".leaf[data-symbol=QQQ]").count() == 1, str(stored()))
    page.reload(); page.wait_for_timeout(800)
    check("기억하기 꺼지면 새로고침 뒤 보유 종목 없음", page.locator(".leaf[data-symbol=QQQ]").count() == 0)
    page.evaluate("() => localStorage.setItem('stockmap.hist.v1', JSON.stringify({VOO: {fetchedAt: Date.now(), bars: [['2026-01-02', 100]], full: true}}))")
    page.reload(); page.wait_for_timeout(800)
    check("기억하기 꺼져 있으면 저장된 과거 가격 캐시 삭제", page.evaluate("() => localStorage.getItem('stockmap.hist.v1')") is None)
    b.close()

server.shutdown()
fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} 통과")
sys.exit(1 if fails else 0)
