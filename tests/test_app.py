"""스톡맵 자동 점검: CSP 위반, XSS 시도, 입력 검증, 실시간(모의) 동작."""
import json, sys
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8765/index.html"
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
    prices = {"VOO": (500, 495), "QQQ": (470, 480), "SCHD": (27, 27.1), "SOXX": (230, 220)}
    def quote(route):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(route.request.url).query)
        s = q["symbol"][0]
        c, pc = prices.get(s, (0, 0))
        route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
                      body=json.dumps({"c": c, "pc": pc, "d": c - pc, "dp": 0}))
    page.route("https://finnhub.io/api/v1/quote*", quote)
    ws_state = {"subs": []}
    def on_ws(ws):
        def on_msg(m):
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
    page.evaluate("h => { localStorage.setItem('stockmap.v1', JSON.stringify({holdings: h})); localStorage.setItem('stockmap.apikey', 'abcdEFGH1234test'); }", holdings)
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
    days = [today - timedelta(days=i) for i in range(1, 45) if (today - timedelta(days=i)).weekday() < 5][:30]
    td_calls = []
    def daily_close(sym, d):  # 날짜마다 다른 가격
        return {"VOO": 400, "QQQ": 500, "SOXX": 200}[sym] + (today - d).days
    def td(route):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(route.request.url).query)
        sym, interval = q["symbol"][0], q["interval"][0]
        td_calls.append((sym, interval, q.get("apikey", [""])[0]))
        if interval == "1day":
            vals = [{"datetime": d.isoformat(), "close": str(daily_close(sym, d))} for d in days]
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
    page.click(".period-btn[data-period='1h']"); page.wait_for_timeout(18000)
    exp = (500 - 112) / 112 * 100  # 새로고침 후 현재가 500(REST), 15:55 기준 1시간 전 = 14:55 → close 100+12
    got = page.text_content(".leaf[data-symbol=VOO] .pct")
    check("1시간 등락 계산", got == f"{exp:+.2f}%", f"{got} vs {exp:+.2f}%")
    page.hover(".leaf[data-symbol=VOO]"); page.wait_for_timeout(200)
    page.screenshot(path="./period.png")
    check("통신 대상이 허용 목록뿐(기간)", set(sent_hosts) <= {"localhost:8765", "finnhub.io", "ws.finnhub.io", "api.twelvedata.com"}, str(set(sent_hosts)))
    check("CSP 위반 없음(기간)", not any("Content Security Policy" in c for c in console), str(console))
    check("alert 실행 안 됨(기간)", not dialogs, str(dialogs))
    page.click(".period-btn[data-period='1w']"); page.wait_for_timeout(500)
    page.mouse.move(5, 5); page.wait_for_timeout(300)
    page.screenshot(path="./week.png")
    b.close()

fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} 통과")
sys.exit(1 if fails else 0)
