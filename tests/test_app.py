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
    check("Twelve Data 키 없으면 환율: 수동", page.text_content("#fxInfo").startswith("환율: 수동"), page.text_content("#fxInfo"))
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
    fx_calls = []
    def daily_close(sym, d):  # 날짜마다 다른 가격
        return {"VOO": 400, "QQQ": 500, "SOXX": 200}[sym] + (today - d).days
    def td(route):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(route.request.url)
        q = parse_qs(u.query)
        if u.path == "/exchange_rate":  # 환율 (앱을 열 때 1번)
            fx_calls.append(q)
            route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
                          body=json.dumps({"symbol": "USD/KRW", "rate": 1385.5, "timestamp": int(datetime.now().timestamp())}))
            return
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
    page.reload(); page.wait_for_timeout(30000)  # 환율 1건 + 일봉 3건, 8초 간격
    target = (datetime.now(ny) - timedelta(days=7)).date()
    base_d = next(d for d in days if d <= target)
    exp = (500 - daily_close("VOO", base_d)) / daily_close("VOO", base_d) * 100
    got = page.text_content(".leaf[data-symbol=VOO] .pct")
    check("1주 등락 계산", got == f"{exp:+.2f}%", f"{got} vs {exp:+.2f}%")
    check("Twelve Data 요청 간격(한도 준수)", len([c for c in td_calls if c[1] == "1day"]) == 3, str(td_calls))
    check("앱을 열 때 환율 1번만 요청", len(fx_calls) == 1, str(len(fx_calls)))
    check("받은 환율 적용·표시", "환율: 자동" in page.text_content("#fxInfo") and "1,385.5원" in page.text_content("#fxInfo"), page.text_content("#fxInfo"))
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
        # 환율 요청: 통화쌍과 키만
        "api.twelvedata.com/exchange_rate": {"symbol": {"USD/KRW"}, "apikey": None},
        "api.twelvedata.com": {"symbol": usd_syms, "interval": {"1day", "5min"}, "outputsize": {"800", "24"},
                               "timezone": {"America/New_York"}, "apikey": None},
    }
    leaks = []
    for url, body in outbound:
        u = urlparse(url)
        rule = allowed_params.get(u.hostname + u.path, allowed_params.get(u.hostname, {}))
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
    fx_out = [url for url, _ in outbound if urlparse(url).path == "/exchange_rate"]
    check("환율 요청도 점검 대상에 잡힘", len(fx_out) == 1, str(len(fx_out)))
    check("환율 요청은 키와 통화쌍(USD/KRW)만", all(set(parse_qs(urlparse(u).query)) == {"symbol", "apikey"}
          and parse_qs(urlparse(u).query)["symbol"] == ["USD/KRW"] for u in fx_out), str([sorted(parse_qs(urlparse(u).query)) for u in fx_out]))
    check("시세 요청에 종목 코드·키·조회 옵션만 들어감(수량·평단·평가액 없음)", not leaks, str(leaks))
    page.click(".period-btn[data-period='1w']"); page.wait_for_timeout(500)
    page.mouse.move(5, 5); page.wait_for_timeout(300)
    page.screenshot(path="./week.png")
    check("기억하기 켜면 과거 가격 캐시 저장", page.evaluate("() => localStorage.getItem('stockmap.hist.v1') !== null"))
    page.click("#settingsBtn"); page.uncheck("#fRememberHoldings"); page.click("#saveSettingsBtn"); page.wait_for_timeout(300)
    page.click("[data-close=settingsDlg]")  # '적용'은 창을 닫지 않아요(결과 문구를 보여 주는 설계). 뒤의 화면 클릭 전에 닫아요.
    st = page.evaluate("() => [localStorage.getItem('stockmap.hist.v1'), 'holdings' in JSON.parse(localStorage.getItem('stockmap.v1') || '{}')]")
    check("기억하기 끄면 과거 가격 캐시도 삭제", st == [None, False], str(st))
    page.click(".period-btn[data-period='1m']"); page.wait_for_timeout(2000)
    check("끄고 나서도 이번 탭에서는 캐시 사용(저장 안 함)", page.text_content(".leaf[data-symbol=VOO] .pct").endswith("%")
          and page.evaluate("() => localStorage.getItem('stockmap.hist.v1')") is None, page.text_content(".leaf[data-symbol=VOO] .pct"))

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
    # 실패 시 화면과 저장소 상태를 보여 주는 상세 값 (키 값은 찍지 않음)
    snapshot_state = lambda: json.dumps(page.evaluate("""() => ({
        mode: document.getElementById('conn').textContent,
        leaves: [...document.querySelectorAll('.leaf')].map(e => e.dataset.symbol),
        localStorage: Object.fromEntries(Object.keys(localStorage).map(k => [k, /key$/.test(k) ? '(키 생략)' : localStorage.getItem(k)])),
        sessionStorage: Object.fromEntries(Object.keys(sessionStorage).map(k => [k, /key$/.test(k) ? '(키 생략)' : sessionStorage.getItem(k)])),
    })"""), ensure_ascii=False)
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
    check("끈 뒤에도 이번 탭에서는 표시(데모 아님)", page.locator(".leaf[data-symbol=VOO]").count() == 1 and page.text_content("#conn") != "데모", snapshot_state())
    # 데모 화면에도 QQQ가 있어서, 데모에 없는 티커(NOTDEMO)로 확인해요.
    page.evaluate("() => localStorage.setItem('stockmap.v1', JSON.stringify({holdings: [{symbol: 'NOTDEMO', currency: 'USD', shares: 1, avgPrice: 1}]}))")
    page.reload(); page.wait_for_timeout(800)
    check("예전 저장값은 불러온 뒤 저장소에서 지움",
          "holdings" not in stored() and page.locator(".leaf[data-symbol=NOTDEMO]").count() == 1, snapshot_state())
    page.reload(); page.wait_for_timeout(800)
    st = json.loads(snapshot_state())
    check("기억하기 꺼지면 새로고침 뒤 보유 종목 없음(화면·localStorage·sessionStorage)",
          "NOTDEMO" not in st["leaves"] and st["mode"] == "데모"
          and "NOTDEMO" not in json.dumps(st["localStorage"]) and "NOTDEMO" not in json.dumps(st["sessionStorage"]),
          json.dumps(st, ensure_ascii=False))
    page.evaluate("() => localStorage.setItem('stockmap.hist.v1', JSON.stringify({VOO: {fetchedAt: Date.now(), bars: [['2026-01-02', 100]], full: true}}))")
    page.reload(); page.wait_for_timeout(800)
    check("기억하기 꺼져 있으면 저장된 과거 가격 캐시 삭제", page.evaluate("() => localStorage.getItem('stockmap.hist.v1')") is None)
    ctx.close()

    # 9) 종목명 표시: 국내(KRW)는 종목명, 이름이 없으면 코드, 미국은 티커
    ctx = b.new_context(viewport={"width": 1280, "height": 720})
    page = ctx.new_page()
    page.goto(BASE)
    names = [
        {"symbol": "000660", "currency": "KRW", "shares": 10, "avgPrice": 180000, "group": "한국", "manualPrice": 205000, "name": "SK하이닉스"},
        {"symbol": "005930", "currency": "KRW", "shares": 30, "avgPrice": 60000, "group": "한국", "manualPrice": 70000},
        {"symbol": "VOO", "currency": "USD", "shares": 4, "avgPrice": 400, "group": "미국", "manualPrice": 500, "name": "Vanguard S&P 500"},
    ]
    page.evaluate("h => localStorage.setItem('stockmap.v1', JSON.stringify({settings: {rememberHoldings: true}, holdings: h}))", names)
    page.reload(); page.wait_for_timeout(1000)
    check("국내 종목 박스에 종목명", page.text_content(".leaf[data-symbol='000660'] .sym") == "SK하이닉스", page.text_content(".leaf[data-symbol='000660'] .sym"))
    check("국내 종목 박스에 숫자 코드 안 보임", "000660" not in page.inner_text(".leaf[data-symbol='000660']"), page.inner_text(".leaf[data-symbol='000660']"))
    check("종목명이 없으면 코드 표시", page.text_content(".leaf[data-symbol='005930'] .sym") == "005930", page.text_content(".leaf[data-symbol='005930'] .sym"))
    check("미국 종목은 이름이 있어도 티커", page.text_content(".leaf[data-symbol=VOO] .sym") == "VOO", page.text_content(".leaf[data-symbol=VOO] .sym"))
    page.hover(".leaf[data-symbol='000660']"); page.wait_for_timeout(200)
    tip = page.inner_text("#tooltip")
    check("국내 종목 툴팁에 종목명, 숫자 코드 없음", page.text_content("#tooltip .tt-title").startswith("SK하이닉스") and "000660" not in tip, tip)
    page.hover(".leaf[data-symbol='005930']"); page.wait_for_timeout(200)
    check("종목명 없는 국내 종목 툴팁은 코드", page.text_content("#tooltip .tt-title").startswith("005930"), page.text_content("#tooltip .tt-title"))
    page.mouse.move(5, 5)
    page.click("#manageBtn")
    page.fill("#fSymbol", "035420"); page.select_option("#fCurrency", "KRW"); page.fill("#fName", "NAVER")
    page.fill("#fShares", "5"); page.fill("#fAvg", "200000"); page.fill("#fManual", "210000")
    page.click("#addForm button[type=submit]"); page.wait_for_timeout(300)
    check("종목명(선택) 칸으로 추가", "NAVER" in page.inner_text("#holdingsBody"), page.inner_text("#holdingsBody"))
    page.evaluate("() => { const i = document.getElementById('fName'); i.removeAttribute('maxlength'); }")
    page.fill("#fSymbol", "373220"); page.select_option("#fCurrency", "KRW"); page.fill("#fName", "가" * 25 + "\u202e")
    page.fill("#fShares", "1"); page.fill("#fAvg", "1")
    page.click("#addForm button[type=submit]"); page.wait_for_timeout(300)
    saved = page.evaluate("() => JSON.parse(localStorage.getItem('stockmap.v1')).holdings.find(h => h.symbol === '373220').name")
    check("종목명 20자 제한·제어 문자 제거", saved == "가" * 20, saved)
    page.click("[data-close=manageDlg]"); page.wait_for_timeout(500)
    check("폼으로 넣은 국내 종목도 이름 표시", page.text_content(".leaf[data-symbol='035420'] .sym") == "NAVER", page.text_content(".leaf[data-symbol='035420'] .sym"))
    ctx.close()

    # 10) 환율 자동/수동 전환
    ctx = b.new_context(viewport={"width": 1280, "height": 720})
    page = ctx.new_page()
    fx_state = {"calls": 0, "fail": False}
    def fx_route(route):
        fx_state["calls"] += 1
        body = {"code": 500, "status": "error", "message": "x"} if fx_state["fail"] else \
               {"symbol": "USD/KRW", "rate": 1385.5, "timestamp": int(datetime.now().timestamp())}
        route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}, body=json.dumps(body))
    page.route("https://api.twelvedata.com/**", fx_route)
    page.goto(BASE)
    one = [{"symbol": "VOO", "currency": "USD", "shares": 1, "avgPrice": 100, "group": "미국", "manualPrice": 100}]
    page.evaluate("h => { localStorage.setItem('stockmap.v1', JSON.stringify({settings: {rememberHoldings: true, fxRate: 1300}, holdings: h})); localStorage.setItem('stockmap.tdkey', 'tdkeyABC123456'); }", one)
    fx_state["calls"] = 0
    page.reload(); page.wait_for_timeout(1500)
    check("자동(기본): 받은 환율로 평가액 계산", page.text_content("#total") == "138,550원", page.text_content("#total"))
    check("자동: 환율과 기준 시각 표시", page.text_content("#fxInfo").startswith("환율: 자동 · 1,385.5원") and "기준" in page.text_content("#fxInfo"), page.text_content("#fxInfo"))
    check("자동: 요청 1번", fx_state["calls"] == 1, str(fx_state["calls"]))
    page.click("#settingsBtn")
    check("환율 자동 스위치 기본 켜짐", page.is_checked("#fFxAuto"))
    page.uncheck("#fFxAuto"); page.click("#saveSettingsBtn"); page.wait_for_timeout(300)
    page.click("[data-close=settingsDlg]"); page.wait_for_timeout(300)
    check("끄면 직접 입력값 사용", page.text_content("#total") == "130,000원", page.text_content("#total"))
    check("끄면 '환율: 수동' 표시", page.text_content("#fxInfo").startswith("환율: 수동") and "직접 입력" in page.text_content("#fxInfo"), page.text_content("#fxInfo"))
    page.click("#settingsBtn"); page.check("#fFxAuto"); page.click("#saveSettingsBtn"); page.wait_for_timeout(300)
    page.click("[data-close=settingsDlg]"); page.wait_for_timeout(300)
    check("다시 켜면 받은 값 사용(새 요청 없음)", page.text_content("#total") == "138,550원" and fx_state["calls"] == 1, f"{page.text_content('#total')}, calls={fx_state['calls']}")
    fx_state["fail"] = True
    page.reload(); page.wait_for_timeout(1500)
    check("실패하면 마지막 값 사용 + '환율: 수동'", page.text_content("#total") == "138,550원"
          and page.text_content("#fxInfo").startswith("환율: 수동") and "지난 자동값" in page.text_content("#fxInfo"), page.text_content("#fxInfo"))
    page.evaluate("() => { const d = JSON.parse(localStorage.getItem('stockmap.v1')); delete d.settings.fxLast; localStorage.setItem('stockmap.v1', JSON.stringify(d)); }")
    page.reload(); page.wait_for_timeout(1500)
    check("실패 + 마지막 값 없음 → 직접 입력값", page.text_content("#total") == "130,000원" and page.text_content("#fxInfo").startswith("환율: 수동"), page.text_content("#fxInfo"))
    ctx.close()

    # 11) 핸드폰 화면 (아이폰 크기 390×844, 좁은 320×640도 글자 넘침 확인)
    for w, h in [(390, 844), (320, 640)]:
        ctx = b.new_context(viewport={"width": w, "height": h}, device_scale_factor=3, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(BASE); page.wait_for_timeout(2000)
        sw = page.evaluate("() => [document.documentElement.scrollWidth, document.body.scrollWidth, window.innerWidth]")
        check(f"{w}px: 가로 스크롤 없음", sw[0] <= sw[2] and sw[1] <= sw[2], str(sw))
        small = page.evaluate("""() => [...document.querySelectorAll('.period-btn, .btn, .icon-btn')]
            .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
            .map(e => { const r = e.getBoundingClientRect(); return [e.textContent.trim(), Math.round(r.width * 10) / 10, Math.round(r.height * 10) / 10]; })
            .filter(([, bw, bh]) => bw < 44 || bh < 44)""")
        check(f"{w}px: 버튼 터치 영역 44×44px 이상", not small, str(small))
        pr = page.evaluate("() => { const p = document.querySelector('.periods').getBoundingClientRect(); return [p.left, p.right, innerWidth]; }")
        check(f"{w}px: 기간 버튼 묶음이 화면 안", pr[0] >= 0 and pr[1] <= pr[2], str(pr))
        over = page.evaluate("""() => [...document.querySelectorAll('.leaf')].flatMap(box => {
            const b = box.getBoundingClientRect();
            return [...box.querySelectorAll('.sym, .pct')].filter(t => t.getClientRects().length && t.textContent).filter(t => {
                const r = t.getBoundingClientRect();
                return r.left < b.left - 1 || r.right > b.right + 1 || r.top < b.top - 1 || r.bottom > b.bottom + 1 || t.scrollWidth > t.clientWidth + 1 && getComputedStyle(t).textOverflow !== 'ellipsis';
            }).map(t => box.dataset.symbol + ':' + t.className);
        })""")
        check(f"{w}px: 박스 글자가 박스 밖으로 안 나감", not over, str(over))
        if w == 390:
            check("390px: 국내 종목 이름표(데모 SK하이닉스)", page.text_content(".leaf[data-symbol='000660'] .sym") in ("SK하이닉스", ""), page.text_content(".leaf[data-symbol='000660'] .sym"))
            syms = page.eval_on_selector_all(".leaf", "els => els.map(e => e.dataset.symbol)")
            bad_tips = []
            for sym in syms:
                page.tap(f".leaf[data-symbol='{sym}']"); page.wait_for_timeout(150)
                t = page.evaluate("() => { const t = document.getElementById('tooltip'); const r = t.getBoundingClientRect(); return [t.hidden, r.left, r.top, r.right, r.bottom, innerWidth, innerHeight]; }")
                if t[0] or t[1] < 0 or t[2] < 0 or t[3] > t[5] or t[4] > t[6]:
                    bad_tips.append((sym, t))
            check("390px: 박스를 탭하면 툴팁이 뜨고 화면 안에 있음(모든 박스)", not bad_tips and len(syms) > 0, str(bad_tips))
            page.tap(".brand"); page.wait_for_timeout(150)
            check("390px: 다른 곳을 탭하면 툴팁 닫힘", page.evaluate("() => document.getElementById('tooltip').hidden"))
            page.screenshot(path="./mobile.png")
        ctx.close()

    # 12) 맥 중계 (국내 종목 시세) — 127.0.0.1:18765를 가짜로 흉내 내요
    ctx = b.new_context(viewport={"width": 1280, "height": 720})
    page = ctx.new_page()
    console = []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
    relay = {"calls": [], "mode": "ok"}
    RT = "0123456789abcdef0123456789abcdef01234567"
    def relay_route(route):
        req = route.request
        cors = {"Access-Control-Allow-Origin": f"http://localhost:{PORT}", "Access-Control-Allow-Headers": "X-Stockmap-Relay",
                "Access-Control-Allow-Methods": "GET", "Content-Type": "application/json"}
        if req.method == "OPTIONS":
            route.fulfill(status=204, headers=cors); return
        relay["calls"].append((req.url, req.headers.get("x-stockmap-relay"), req.headers.get("cookie")))
        if relay["mode"] == "down":
            route.abort(); return
        if relay["mode"] == "auth":
            route.fulfill(status=401, headers=cors, body=""); return
        body = {"ok": True, "status": "", "fetchedAt": "2026-11-13T10:00:00+09:00",
                "prices": [{"symbol": "000660", "price": 1233000, "at": "2026-11-13T10:00:00+09:00", "prevClose": 1200000},
                           {"symbol": "999999", "price": 5, "prevClose": 1},          # 요청 안 한 종목 → 무시
                           {"symbol": "005930", "price": "<b>", "prevClose": None}]}  # 이상한 값 → 무시
        route.fulfill(status=200, headers=cors, body=json.dumps(body))
    page.route("http://127.0.0.1:18765/**", relay_route)
    page.goto(BASE)
    kr = [{"symbol": "000660", "currency": "KRW", "shares": 2, "avgPrice": 1000000, "group": "한국", "manualPrice": 1200000, "name": "SK하이닉스"},
          {"symbol": "005930", "currency": "KRW", "shares": 1, "avgPrice": 60000, "group": "한국", "manualPrice": 70000}]
    page.evaluate("h => localStorage.setItem('stockmap.v1', JSON.stringify({settings: {rememberHoldings: true}, holdings: h}))", kr)
    page.reload(); page.wait_for_timeout(2500)
    check("중계 토큰 없으면 127.0.0.1로 요청 안 함", not relay["calls"], str(relay["calls"]))
    check("중계 토큰 없으면 국내 종목은 수동 가격", page.text_content(".leaf[data-symbol='000660'] .pct") == "수동 가격", page.text_content(".leaf[data-symbol='000660'] .pct"))
    check("미국 종목 없고 Finnhub 키 없으면 '미국 종목 없음'(키 오류 아님)", page.text_content("#conn") == "미국 종목 없음", page.text_content("#conn"))
    page.click("#settingsBtn")
    page.fill("#fRelay", "not-a-token"); page.click("#relayForm button[type=submit]")
    check("잘못된 중계 토큰 거부", "형식" in page.text_content("#settingsMsg"), page.text_content("#settingsMsg"))
    page.fill("#fRelay", RT); page.click("#relayForm button[type=submit]")
    check("중계 토큰 저장 후 '저장됨'만 표시", page.text_content("#relayStatus") == "저장됨" and page.input_value("#fRelay") == "", page.text_content("#relayStatus"))
    page.click("[data-close=settingsDlg]"); page.wait_for_timeout(3000)
    pct = page.text_content(".leaf[data-symbol='000660'] .pct")
    check("중계 가격 반영 (1,233,000 vs 전일 1,200,000 → +2.75%)", pct == "+2.75%", pct)
    check("중계 배지 '국내 실시간'", page.text_content("#relay") == "국내 실시간" and page.is_visible("#relay"), page.text_content("#relay"))
    check("이상한 중계 값은 무시(수동 가격 유지)", page.text_content(".leaf[data-symbol='005930'] .pct") == "수동 가격", page.text_content(".leaf[data-symbol='005930'] .pct"))
    from urllib.parse import urlparse, parse_qs
    q = [parse_qs(urlparse(u).query) for u, _, _ in relay["calls"]]
    check("중계 요청엔 종목 코드만 (수량·평단 없음)", all(set(x) == {"symbols"} and x["symbols"] == ["000660,005930"] for x in q), str(q))
    check("중계 요청에 토큰 헤더, 쿠키 없음", all(t == RT and c is None for _, t, c in relay["calls"]), str([(t == RT, c) for _, t, c in relay["calls"]]))
    check("중계는 2초 간격", 1 <= len(relay["calls"]) <= 3, str(len(relay["calls"])))
    page.click(".period-btn[data-period='1w']"); page.wait_for_timeout(1200)
    check("국내 종목 1주 등락은 '데이터 없음'", page.text_content(".leaf[data-symbol='000660'] .pct") == "데이터 없음", page.text_content(".leaf[data-symbol='000660'] .pct"))
    page.click(".period-btn[data-period='day']"); page.wait_for_timeout(500)
    relay["mode"] = "auth"; page.wait_for_timeout(2500)
    check("토큰이 틀리면 '중계 토큰 오류'", page.text_content("#relay") == "중계 토큰 오류", page.text_content("#relay"))
    relay["mode"] = "down"; page.wait_for_timeout(7000)
    check("중계가 꺼지면 '맥 중계 꺼짐'", page.text_content("#relay") == "맥 중계 꺼짐", page.text_content("#relay"))
    check("CSP 위반 없음(중계)", not any("Content Security Policy" in c for c in console), str(console))
    page.click("#settingsBtn"); page.uncheck("#fRemember"); page.click("#saveSettingsBtn"); page.wait_for_timeout(300)
    st = page.evaluate("() => [localStorage.getItem('stockmap.relaytoken'), sessionStorage.getItem('stockmap.relaytoken') !== null]")
    check("키 기억 끄면 중계 토큰도 세션에만", st == [None, True], str(st))
    page.click("#deleteRelayBtn"); page.wait_for_timeout(300)
    n = len(relay["calls"]); page.wait_for_timeout(3000)
    check("중계 토큰 삭제하면 요청 멈춤·배지 숨김", len(relay["calls"]) == n and not page.is_visible("#relay"), f"{n} → {len(relay['calls'])}")
    ctx.close()
    b.close()

server.shutdown()
fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} 통과")
sys.exit(1 if fails else 0)
