#!/usr/bin/env python3
"""룰루루 P2 보강: 시그널 종목 + Core(미장)만 옵션·신용·공시·뉴스·실적 일정을 붙인다.

- 원칙: 소스가 실패하면 값을 지어내지 않고 status에 실패 사유를 남긴다("미확보").
- 표준 라이브러리만 사용. screener.py가 import 해서 쓴다.
- 소스
  * 옵션: CBOE 지연시세 JSON  https://cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json  (15분 지연)
  * 신용: FRED CSV           BAMLH0A0HYM2(HY OAS), BAMLC0A0CM(IG OAS)
  * 공시: SEC EDGAR           company_tickers.json + submissions (환경변수 SEC_USER_AGENT 필수)
  * 뉴스: Yahoo Finance RSS   종목별 헤드라인 최근 24시간 3건
  * 실적: Nasdaq 실적 캘린더   앞으로 14일 + 직전 3일
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
OCC_RE = re.compile(r"^(.*?)(\d{6})([CP])(\d{8})$")
EDGAR_FORMS_ISSUE = {"424B2", "424B3", "424B5", "FWP"}
ITEM_KO = {"1.01": "중요계약", "1.02": "계약해지", "2.01": "인수·처분", "2.02": "실적", "2.03": "채무발생",
           "2.05": "구조조정", "2.06": "손상", "3.02": "지분발행", "5.02": "임원변동", "7.01": "공정공시",
           "8.01": "기타"}


class SourceError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def http_get(url: str, headers: dict | None = None, timeout: float = 20) -> bytes:
    h = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    h.update(headers or {})
    try:
        with urlopen(Request(url, headers=h), timeout=timeout) as r:
            return r.read()
    except HTTPError as e:
        raise SourceError(f"http_{e.code}") from e
    except (socket.timeout, TimeoutError) as e:
        raise SourceError("timeout") from e
    except URLError as e:
        raise SourceError("network_error") from e
    except OSError as e:
        raise SourceError("network_error") from e


# ============================================================== options (CBOE)
def third_friday(y: int, m: int) -> date:
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def parse_cboe(payload: dict, today: date, window_days: int = 45) -> dict:
    data = payload.get("data") or {}
    spot = data.get("current_price") or data.get("close")
    opts = data.get("options") or []
    if not spot or not opts:
        raise SourceError("empty_chain")
    spot = float(spot)
    rows = []
    for o in opts:
        m = OCC_RE.match(str(o.get("option", "")))
        if not m:
            continue
        exp = datetime.strptime(m.group(2), "%y%m%d").date()
        dte = (exp - today).days
        if dte < 0:
            continue
        rows.append({"exp": exp, "dte": dte, "cp": m.group(3), "k": int(m.group(4)) / 1000.0,
                     "oi": float(o.get("open_interest") or 0), "vol": float(o.get("volume") or 0),
                     "iv": float(o.get("iv") or 0), "gamma": float(o.get("gamma") or 0)})
    near = [r for r in rows if r["dte"] <= window_days]
    if not near:
        raise SourceError("no_near_expiry")
    call_oi, put_oi = {}, {}
    for r in near:
        (call_oi if r["cp"] == "C" else put_oi)[r["k"]] = (call_oi if r["cp"] == "C" else put_oi).get(r["k"], 0) + r["oi"]
    call_wall = max(call_oi, key=call_oi.get) if call_oi else None
    put_wall = max(put_oi, key=put_oi.get) if put_oi else None
    tc, tp = sum(call_oi.values()), sum(put_oi.values())

    # 맥스페인: 가장 가까운 월물(셋째 금요일) — 없으면 가장 가까운 만기
    exps = sorted({r["exp"] for r in rows})
    monthly = [e for e in exps if e == third_friday(e.year, e.month) or e == third_friday(e.year, e.month) - timedelta(days=1)]
    mp_exp = (monthly or exps)[0]
    chain = [r for r in rows if r["exp"] == mp_exp]
    strikes = sorted({r["k"] for r in chain})
    max_pain = None
    if strikes:
        def pain(p):
            return sum(r["oi"] * (max(0, p - r["k"]) if r["cp"] == "C" else max(0, r["k"] - p)) for r in chain)
        max_pain = min(strikes, key=pain)

    # ATM IV·기대변동: 30일에 가장 가까운 만기(7일 이상)
    cand = [e for e in exps if (e - today).days >= 7] or exps
    iv_exp = min(cand, key=lambda e: abs((e - today).days - 30))
    ivc = [r for r in rows if r["exp"] == iv_exp and r["iv"] > 0]
    atm_iv = em = None
    if ivc:
        k0 = min({r["k"] for r in ivc}, key=lambda k: abs(k - spot))
        ivs = [r["iv"] for r in ivc if r["k"] == k0]
        atm_iv = sum(ivs) / len(ivs)
        dte = max((iv_exp - today).days, 1)
        em = spot * atm_iv * math.sqrt(dte / 365)

    gex = sum(r["gamma"] * r["oi"] * 100 * spot * spot * 0.01 * (1 if r["cp"] == "C" else -1) for r in near)
    unusual = sorted([r for r in rows if r["vol"] >= 500 and r["vol"] >= 3 * max(r["oi"], 1)],
                     key=lambda r: -r["vol"])[:2]
    return {
        "spot": round(spot, 4), "window_days": window_days,
        "call_wall": call_wall, "put_wall": put_wall,
        "max_pain": max_pain, "max_pain_exp": mp_exp.isoformat(),
        "pc_oi": round(tp / tc, 2) if tc else None,
        "atm_iv_pct": round(atm_iv * 100, 1) if atm_iv else None,
        "exp_move": round(em, 2) if em else None, "exp_move_pct": round(em / spot * 100, 1) if em else None,
        "iv_exp": iv_exp.isoformat(),
        "gex_usd_bn_per_1pct": round(gex / 1e9, 3), "gex_note": "추정(딜러 콜 매수·풋 매도 가정)",
        "unusual": [f"{r['exp'].strftime('%m/%d')} {r['k']:g}{r['cp']} 거래 {int(r['vol']):,} (OI {int(r['oi']):,})"
                    for r in unusual],
    }


def fetch_options(sym: str, today: date, getter=http_get) -> dict:
    root = sym.replace("-", ".")
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{quote(root)}.json"
    raw = getter(url, None, 45) if getter is http_get else getter(url)  # 대형주 체인은 수 MB
    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise SourceError("invalid_json") from e
    out = parse_cboe(payload, today)
    out["as_of"] = payload.get("timestamp")
    return out


# ============================================================== credit (FRED)
def fetch_fred(series: str, getter=http_get) -> dict:
    raw = getter(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}").decode("utf-8", "replace")
    rows = [r for r in csv.reader(io.StringIO(raw))][1:]
    vals = [(r[0], float(r[1])) for r in rows if len(r) >= 2 and r[1] not in (".", "")]
    if len(vals) < 6:
        raise SourceError("too_few_points")
    d, v = vals[-1]
    d5, v5 = vals[-6]
    return {"series": series, "as_of": d, "value_pct": v, "chg_1w_bp": round((v - v5) * 100, 1)}


# ============================================================== EDGAR
class Edgar:
    def __init__(self, ua: str | None, getter=http_get):
        self.ua, self.get, self._map = ua, getter, None

    def cik(self, sym: str):
        if self._map is None:
            raw = self.get("https://www.sec.gov/files/company_tickers.json", {"User-Agent": self.ua})
            self._map = {v["ticker"].upper(): int(v["cik_str"]) for v in json.loads(raw).values()}
        return self._map.get(sym.upper().replace(".", "-"))

    def recent(self, sym: str, since: date) -> list[str]:
        c = self.cik(sym)
        if not c:
            raise SourceError("no_cik")
        time.sleep(0.15)  # SEC 10 req/s 제한
        raw = self.get(f"https://data.sec.gov/submissions/CIK{c:010d}.json", {"User-Agent": self.ua})
        rec = (json.loads(raw).get("filings") or {}).get("recent") or {}
        out, n4 = [], 0
        for form, fd, items in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("items", [""] * 9999)):
            if fd < since.isoformat():
                break
            if form == "4":
                n4 += 1
            elif form in ("8-K", "6-K"):
                labels = [ITEM_KO.get(i.strip(), i.strip()) for i in (items or "").split(",") if i.strip() and i.strip() != "9.01"]
                out.append(f"{fd[5:]} {form}" + (f"({'·'.join(labels)})" if labels else ""))
            elif form in EDGAR_FORMS_ISSUE:
                out.append(f"{fd[5:]} 증권발행 {form}")
        if n4:
            out.append(f"내부자 Form4 {n4}건")
        return out[:4]


# ============================================================== news (Yahoo RSS)
def fetch_news(sym: str, now_utc: datetime, getter=http_get, hours: int = 24) -> list[dict]:
    raw = getter(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote(sym)}&region=US&lang=en-US")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise SourceError("invalid_xml") from e
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if title and now_utc - ts <= timedelta(hours=hours):
            out.append({"t": ts.astimezone(timezone(timedelta(hours=9))).strftime("%m/%d %H:%M"),
                        "title": title[:140], "link": (it.findtext("link") or "").strip()})
    return out[:3]


# ============================================================== earnings (Nasdaq)
def fetch_earnings(days: list[date], getter=http_get) -> dict:
    out, ok = {}, 0
    for d in days:
        try:
            raw = getter(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}",
                         {"Accept": "application/json", "Origin": "https://www.nasdaq.com",
                          "Referer": "https://www.nasdaq.com/"})
            rows = ((json.loads(raw).get("data") or {}).get("rows")) or []
            ok += 1
        except (SourceError, ValueError):
            continue
        for r in rows:
            s = str(r.get("symbol") or "").upper()
            if s and s not in out:
                t = str(r.get("time") or "")
                out[s] = {"date": d.isoformat(),
                          "time": "장후" if "after" in t else ("장전" if "pre" in t else "")}
        time.sleep(0.3)
    if ok == 0:
        raise SourceError("all_days_failed")
    return out


# ============================================================== orchestrate
def enrich(market: str, symbols: list[str], session: date, now_utc: datetime, getter=http_get) -> dict:
    res = {"tickers": {s: {} for s in symbols}, "credit": {}, "sources": {}}
    if market != "US":
        res["sources"] = {"options": "해당없음(국장)", "edgar": "해당없음(국장)", "news": "미확보(국장)",
                          "earnings": "미확보(국장)", "credit": "US 회차에서 갱신"}
        return res

    def tally(name, ok, fail):
        res["sources"][name] = "ok" if not fail else (f"일부실패 {len(fail)}/{ok + len(fail)}" if ok else
                                                       f"미확보({next(iter(fail.values()))})")

    ok, fail = 0, {}
    for s in symbols:
        try:
            res["tickers"][s]["options"] = fetch_options(s, now_utc.date(), getter)
            ok += 1
        except SourceError as e:
            fail[s] = e.code
        time.sleep(0.3)
    tally("options", ok, fail)

    ok, fail = 0, {}
    for sid, key in (("BAMLH0A0HYM2", "hy"), ("BAMLC0A0CM", "ig")):
        try:
            res["credit"][key] = fetch_fred(sid, getter)
            ok += 1
        except SourceError as e:
            fail[sid] = e.code
    tally("credit", ok, fail)

    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        res["sources"]["edgar"] = "미확보(SEC_USER_AGENT 미설정)"
    else:
        ed, ok, fail = Edgar(ua, getter), 0, {}
        for s in symbols:
            try:
                res["tickers"][s]["filings"] = ed.recent(s, session - timedelta(days=2))
                ok += 1
            except SourceError as e:
                fail[s] = e.code
            except (ValueError, KeyError):
                fail[s] = "parse_error"
        tally("edgar", ok, fail)

    ok, fail = 0, {}
    for s in symbols:
        try:
            res["tickers"][s]["news"] = fetch_news(s, now_utc, getter)
            ok += 1
        except SourceError as e:
            fail[s] = e.code
        time.sleep(0.2)
    tally("news", ok, fail)

    days = [session + timedelta(days=k) for k in range(-3, 15) if (session + timedelta(days=k)).weekday() < 5]
    try:
        cal = fetch_earnings(days, getter)
        for s in symbols:
            if s.upper() in cal:
                res["tickers"][s]["earnings"] = cal[s.upper()]
        res["sources"]["earnings"] = "ok"
    except SourceError as e:
        res["sources"]["earnings"] = f"미확보({e.code})"
    return res


# ============================================================== text helpers (screener가 사용)
def suffix(info: dict, session: date, market: str) -> str:
    parts = []
    er = info.get("earnings")
    if er:
        d = date.fromisoformat(er["date"])
        if session - timedelta(days=3) <= d <= session + timedelta(days=10):
            parts.append(f"실적 {d.month}/{d.day}{er.get('time', '')}")
    op = info.get("options")
    if op and op.get("call_wall") and op.get("put_wall"):
        parts.append(f"콜월 {op['call_wall']:g}/풋월 {op['put_wall']:g}")
    return (" · " + " · ".join(parts)) if parts else ""


def credit_line(res: dict) -> str:
    c = res.get("credit") or {}
    seg = []
    for key, lab in (("hy", "HY"), ("ig", "IG")):
        if key in c:
            seg.append(f"{lab} {c[key]['value_pct']:.2f}%({c[key]['chg_1w_bp']:+.0f}bp/1주)")
    src = res.get("sources") or {}
    head = "신용: " + (" ".join(seg) if seg else f"미확보({src.get('credit', '미실행')})")
    return head + " · 개별 채권·CDS: 미확보"


def sources_line(res: dict) -> str:
    s = res.get("sources") or {}
    names = {"options": "옵션", "edgar": "공시", "news": "뉴스", "earnings": "실적"}
    return "보강: " + ", ".join(f"{names[k]} {s.get(k, '미실행')}" for k in names)


# ============================================================== selftest
def selftest() -> list[str]:
    errs = []
    today = date(2026, 11, 4)
    opts = []
    for exp in ("261106", "261120", "261218"):
        for k in (180, 190, 200, 210, 220):
            for cp in "CP":
                oi = (5000 if (cp == "C" and k == 220) else 4000 if (cp == "P" and k == 180) else 1000)
                opts.append({"option": f"NVDA{exp}{cp}{k * 1000:08d}", "open_interest": oi,
                             "volume": 9000 if (exp == "261106" and k == 210 and cp == "C") else 100,
                             "iv": 0.5, "gamma": 0.01})
    r = parse_cboe({"data": {"current_price": 200.0, "options": opts}}, today)
    if r["call_wall"] != 220 or r["put_wall"] != 180:
        errs.append(f"walls {r['call_wall']} {r['put_wall']}")
    if r["max_pain_exp"] != "2026-11-20":
        errs.append(f"max pain exp {r['max_pain_exp']}")
    if not r["unusual"] or "210C" not in r["unusual"][0]:
        errs.append(f"unusual {r['unusual']}")
    if not r["exp_move"]:
        errs.append("exp move")
    csvraw = "observation_date,X\n" + "\n".join(f"2026-10-{d:02d},{2.5 + d / 100:.2f}" for d in range(20, 31)) + "\n2026-10-31,.\n"
    f = fetch_fred("X", getter=lambda u, h=None: csvraw.encode())
    if f["as_of"] != "2026-10-30" or f["chg_1w_bp"] != 5.0:
        errs.append(f"fred {f}")
    rss = (b"<rss><channel><item><title>NVDA rallies</title><pubDate>Wed, 04 Nov 2026 01:00:00 GMT</pubDate>"
           b"<link>x</link></item><item><title>old</title><pubDate>Fri, 30 Oct 2026 01:00:00 GMT</pubDate></item>"
           b"</channel></rss>")
    n = fetch_news("NVDA", datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc), getter=lambda u, h=None: rss)
    if len(n) != 1:
        errs.append(f"news {n}")

    def bad(u, h=None):
        raise SourceError("http_403")
    os.environ.pop("SEC_USER_AGENT", None)
    res = enrich("US", ["NVDA"], today, datetime(2026, 11, 4, 21, 30, tzinfo=timezone.utc), getter=bad)
    if "미확보" not in res["sources"]["options"] or "SEC_USER_AGENT" not in res["sources"]["edgar"]:
        errs.append(f"fail path {res['sources']}")
    if "미확보" not in credit_line(res):
        errs.append("credit line")
    return errs
