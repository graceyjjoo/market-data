#!/usr/bin/env python3
"""룰루루 시그널 스크리너 (P1).

- 종목 목록은 config/universe.json에서만 읽는다 (코드에 종목 없음).
- Yahoo 일봉(v8 chart, interval=1d)을 받아 시그널을 판정하고 market/signals/ 에만 쓴다.
- 기존 시간별 수집(market/yahoo/)은 읽지도 쓰지도 않는다.
- 표준 라이브러리만 사용한다 (pip 설치 불필요). Python 3.10+.

사용:
  python screener.py --market US --config config/universe.json --out market/signals
  python screener.py --market KR ... --dry-run      # 아무것도 저장·발송하지 않고 결과만 출력
  python screener.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

KST = timezone(timedelta(hours=9))
BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
ACCESS_LIMIT_CODES = {401, 403, 429}
SERVER_ERROR_CODES = {500, 502, 503, 504}

DEFAULT_PARAMS = {
    "vol_mult": 1.5, "touch_atr": 0.25, "guillotine_atr": 1.0, "guillotine_exit_atr": 1.5,
    "ema_cluster_atr": 0.5, "big_red_pct": -3.0, "gap_pct": 4.0, "gap_vol_mult": 2.0,
    "breakout_lookback": 55, "confirm_days": 3, "cooldown_sessions": 5, "theme_min": 3,
    "push_max": 8, "brief_max": 5, "digest_max_bytes": 2048, "history_range": "2y",
    "min_bars": 60, "score_horizons": [5, 20], "guillotine_eval_sessions": 10,
    "ema50_break_vol_mult": 0.0, "failed_high_vol_mult": 0.0,
    "index_types": ["BREAKDOWN", "WARNING", "BREAKOUT_CONFIRMED", "BREAKOUT_CANDIDATE", "BOUNCE", "GAP"],
}

# 정렬: cat(① 위험 → ② 돌파 → ③ 반등/후보 → ④ 갭 → ⑤ 테마) → sub → 층(core>watch>index) → 점수
TYPES = {
    "BREAKDOWN":          {"cat": 1, "sub": 0, "fam": "risk", "sev": 3, "label": "이탈"},
    "GUILLOTINE":         {"cat": 1, "sub": 1, "fam": "risk", "sev": 1, "label": "단두대"},
    "WARNING":            {"cat": 1, "sub": 2, "fam": "risk", "sev": 2, "label": "조짐"},
    "BREAKOUT_CONFIRMED": {"cat": 2, "sub": 0, "fam": "bull", "sev": 3, "label": "돌파확정"},
    "BREAKOUT_CANDIDATE": {"cat": 3, "sub": 0, "fam": "bull", "sev": 2, "label": "돌파후보"},
    "BOUNCE":             {"cat": 3, "sub": 1, "fam": "bull", "sev": 1, "label": "반등"},
    "GAP":                {"cat": 4, "sub": 0, "fam": "gap",  "sev": 1, "label": "갭"},
    "THEME":              {"cat": 5, "sub": 0, "fam": "theme", "sev": 1, "label": "테마동반"},
    "BREAKOUT_FAILED":    {"cat": 9, "sub": 0, "fam": "info", "sev": 0, "label": "돌파무산"},  # digest 한 줄만, 푸시 없음
}
TIER_RANK = {"core": 0, "watch": 1, "index": 2}
TIER_LABEL = {"core": "Core", "watch": "Watch", "index": "Index"}
BULL_TYPES = {"BOUNCE", "BREAKOUT_CANDIDATE", "BREAKOUT_CONFIRMED"}
BEAR_TYPES = {"BREAKDOWN", "WARNING"}


# ============================================================== config
class ConfigError(Exception):
    pass


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    for key in ("markets", "tiers"):
        if key not in cfg:
            raise ConfigError(f"universe.json에 '{key}'가 없습니다")
    for tier in cfg["tiers"]:
        if tier not in TIER_RANK:
            raise ConfigError(f"알 수 없는 층: {tier} (core/watch/index만 가능)")
    for tier, items in cfg["tiers"].items():
        for it in items:
            if "symbol" not in it:
                continue  # _source 같은 설명 줄
            if it.get("market") not in cfg["markets"]:
                raise ConfigError(f"{it['symbol']}: market은 {list(cfg['markets'])} 중 하나여야 합니다")
    return cfg


def build_universe(cfg: dict, market: str) -> dict:
    """symbol -> {symbol,name,tier,theme}. 위 층(core>watch>index)이 우선."""
    out: dict[str, dict] = {}
    for tier in ("core", "watch", "index"):
        for it in cfg["tiers"].get(tier, []):
            sym = it.get("symbol")
            if not sym or it.get("market") != market or sym in out:
                continue
            out[sym] = {"symbol": sym, "name": it.get("name") or sym, "tier": tier,
                        "theme": it.get("theme") or ""}
    return out


def params_of(cfg: dict) -> dict:
    p = dict(DEFAULT_PARAMS)
    p.update({k: v for k, v in cfg.get("params", {}).items() if not k.startswith("_")})
    return p


# ============================================================== fetch
class FetchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _http_get_json(url: str, timeout: float) -> dict:
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as exc:
        if exc.code in ACCESS_LIMIT_CODES:
            raise FetchError("access_limit", f"HTTP {exc.code}") from exc
        if exc.code in SERVER_ERROR_CODES:
            raise FetchError("server_error", f"HTTP {exc.code}") from exc
        raise FetchError("http_error", f"HTTP {exc.code}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise FetchError("timeout", str(exc)) from exc
    except URLError as exc:
        raise FetchError("network_error", str(exc.reason)) from exc
    except OSError as exc:
        raise FetchError("network_error", str(exc)) from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise FetchError("invalid_json", str(exc)) from exc


def parse_chart(symbol: str, payload: dict) -> dict:
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise FetchError("malformed_response", "missing chart")
    if chart.get("error"):
        raise FetchError("yahoo_error", json.dumps(chart["error"], ensure_ascii=False)[:200])
    res = chart.get("result")
    if not isinstance(res, list) or not res:
        raise FetchError("malformed_response", "missing result")
    r0 = res[0]
    meta = r0.get("meta") or {}
    if str(meta.get("symbol") or "").upper() != symbol.upper():
        raise FetchError("symbol_mismatch", f"requested {symbol}, got {meta.get('symbol')}")
    ts = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    off = int(meta.get("gmtoffset") or 0)
    by_date: dict[date, tuple] = {}
    for k, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][k], q["high"][k], q["low"][k], q["close"][k]
            v = q.get("volume", [None] * len(ts))[k]
        except (KeyError, IndexError, TypeError):
            continue
        if None in (o, h, l, c):
            continue
        d = datetime.fromtimestamp(int(t) + off, tz=timezone.utc).date()
        by_date[d] = (d, float(o), float(h), float(l), float(c), float(v or 0))
    bars = [by_date[d] for d in sorted(by_date)]
    if not bars:
        raise FetchError("no_bars", "no usable daily bars")
    return {"bars": bars, "gmtoffset": off, "currency": meta.get("currency") or ""}


def yahoo_daily(symbol: str, range_: str, timeout: float = 10) -> dict:
    url = BASE_URL + quote(symbol, safe="") + "?" + urlencode({"interval": "1d", "range": range_})
    return parse_chart(symbol, _http_get_json(url, timeout))


def drop_partial_bar(data: dict, close_local: str, settle_min: int, now_utc: datetime) -> dict:
    """장 마감 + settle 이전에 받은 '오늘' 봉은 미완성이므로 버린다."""
    off = data["gmtoffset"]
    now_local = now_utc + timedelta(seconds=off)
    hh, mm = (int(x) for x in close_local.split(":"))
    cutoff = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=settle_min)
    bars = data["bars"]
    if bars and bars[-1][0] == now_local.date() and now_local < cutoff:
        data = dict(data, bars=bars[:-1], dropped_partial=True)
    return data


def fetch_all(symbols: list[str], range_: str, market_cfg: dict, now_utc: datetime,
              fetcher=yahoo_daily, sleep=time.sleep, delay: float = 0.6,
              access_wait: float = 15) -> tuple[dict, dict]:
    ok: dict[str, dict] = {}
    failed: dict[str, str] = {}
    blocked = False
    for i, sym in enumerate(symbols):
        if blocked:
            failed[sym] = "not_attempted"
            continue
        if i:
            sleep(delay)
        tries, access_retry = 0, False
        while True:
            tries += 1
            try:
                d = fetcher(sym, range_)
                ok[sym] = drop_partial_bar(d, market_cfg["close_local"],
                                           int(market_cfg.get("settle_minutes", 20)), now_utc)
                break
            except FetchError as e:
                if e.code == "access_limit" and not access_retry:
                    access_retry = True
                    sleep(access_wait)
                    continue
                if e.code == "access_limit":
                    blocked = True
                    failed[sym] = e.code
                    break
                if e.code in ("server_error", "timeout", "network_error") and tries < 2:
                    sleep(2)
                    continue
                failed[sym] = e.code
                break
    return ok, failed


# ============================================================== indicators
def sma(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    a = 2.0 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * a + e * (1 - a)
        out[i] = e
    return out


def atr(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                          for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def indicators(bars, p) -> dict:
    o = [b[1] for b in bars]; h = [b[2] for b in bars]; l = [b[3] for b in bars]
    c = [b[4] for b in bars]; v = [b[5] for b in bars]
    return {"dates": [b[0] for b in bars], "o": o, "h": h, "l": l, "c": c, "v": v,
            "ema20": ema(c, 20), "ema50": ema(c, 50), "sma200": sma(c, 200), "atr": atr(h, l, c, 14)}


# ============================================================== session helpers
def sessions_between(bench_dates: list[date], d1: str, d2: date) -> int:
    """d1(제외) ~ d2(포함) 사이 거래일 수 (벤치마크 일봉 날짜 기준)."""
    a = date.fromisoformat(d1)
    return sum(1 for d in bench_dates if a < d <= d2)


def fmt_px(x: float, market: str) -> str:
    return f"{x:,.0f}" if market == "KR" else f"{x:,.2f}"


# ============================================================== evaluation (종목 1개)
def evaluate(sym: str, mem: dict, ind: dict, p: dict, levels: list, st: dict,
             market: str, bench_dates: list[date]) -> list[dict]:
    i = len(ind["c"]) - 1
    if i + 1 < p["min_bars"]:
        return []
    d = ind["dates"][i]
    c, o, h, l, v = ind["c"][i], ind["o"][i], ind["h"][i], ind["l"][i], ind["v"][i]
    pc = ind["c"][i - 1]
    a = ind["atr"][i]
    if not a or a <= 0:
        return []
    prev_vols = [x for x in ind["v"][max(0, i - 20):i] if x > 0]
    vx = (v / (sum(prev_vols) / len(prev_vols))) if prev_vols and v > 0 else 0.0
    chg = (c / pc - 1) * 100
    e20, e50, s200 = ind["ema20"][i], ind["ema50"][i], ind["sma200"][i]
    e50p, s200p = ind["ema50"][i - 1], ind["sma200"][i - 1]
    lb = int(p["breakout_lookback"])
    hN = max(ind["h"][i - lb:i]) if i >= lb else None
    vm = p["vol_mult"]
    vtxt = f"거래량 {vx:.1f}×"

    supports = []  # (name, today_value, yesterday_value)
    if s200 is not None and s200p is not None:
        supports.append(("200SMA", s200, s200p))
    for lv in levels:
        if lv.get("kind") == "support":
            supports.append((lv.get("name", "지지선"), float(lv["price"]), float(lv["price"])))

    base = {"symbol": sym, "name": mem["name"], "tier": mem["tier"], "theme": mem["theme"],
            "session": d.isoformat(), "close": round(c, 4), "chg_pct": round(chg, 2),
            "atr": round(a, 4), "vol_x": round(vx, 2)}

    def ev(typ, reason, level_name=None, level=None, extra=0.0):
        tier_bonus = {"core": 2, "watch": 1, "index": 0}[mem["tier"]]
        score = round(min(vx, 5) + abs(chg) / 2 + tier_bonus + extra, 2)
        return dict(base, type=typ, reason=reason, level_name=level_name,
                    level=None if level is None else round(level, 4), score=score)

    events: list[dict] = []
    gst = st.setdefault("guillotine", {})
    gex = st.setdefault("guillotine_exit", {})
    bst = st.setdefault("breakout", {})
    in_g = gst.get(sym)

    # ---- 단두대 후보 기준선 (Core·Watch만)
    g_levels = []
    if mem["tier"] in ("core", "watch"):
        if s200 is not None:
            g_levels.append(("200SMA", s200))
        if e20 is not None and e50 is not None and abs(e20 - e50) <= p["ema_cluster_atr"] * a:
            g_levels.append(("20/50EMA 중첩", min(e20, e50)))
        for lv in levels:
            if lv.get("kind") == "support":
                g_levels.append((lv.get("name", "지지선"), float(lv["price"])))
    g_now = {n: val for n, val in g_levels}

    # ---- ① 이탈 경고
    breakdown = None
    for name, lt, ly in supports:
        if pc >= ly and c < lt and vx >= vm:
            breakdown = ev("BREAKDOWN", f"{name} {fmt_px(lt, market)} 하회 · {vtxt}", name, lt, extra=2)
            break
    if breakdown is None and in_g:
        lname = in_g["level_name"]
        lval = g_now.get(lname)
        if lval is None and lname == "200SMA" and s200 is not None:
            lval = s200
        if lval is not None and c < lval:
            tag = "" if vx >= vm else " (거래량 미동반)"
            breakdown = ev("BREAKDOWN", f"단두대→이탈 {lname} {fmt_px(lval, market)} 하회 · {vtxt}{tag}",
                           lname, lval, extra=2)
    if breakdown:
        events.append(breakdown)
        if in_g:
            gex[sym] = d.isoformat()
        gst.pop(sym, None)
        in_g = None

    # ---- ② 단두대 (진입 1회 알림, 머무는 동안 조용, 위로 1.5ATR 벗어나면 해제)
    if breakdown is None and g_levels:
        near = [(n, val, (c - val) / a) for n, val in g_levels if 0 <= (c - val) / a <= p["guillotine_atr"]]
        if in_g:
            cur = g_now.get(in_g["level_name"])
            dist = None if cur is None else (c - cur) / a
            if near:
                n, val, dd = min(near, key=lambda x: x[2])
                in_g.update(level_name=n, level=round(val, 4))
            elif dist is None or dist > p["guillotine_exit_atr"]:
                gst.pop(sym, None)
                gex[sym] = d.isoformat()
        elif near:
            n, val, dd = min(near, key=lambda x: x[2])
            gst[sym] = {"level_name": n, "level": round(val, 4), "since": d.isoformat()}
            last_exit = gex.get(sym)
            recent = last_exit and sessions_between(bench_dates, last_exit, d) <= p["cooldown_sessions"]
            if not recent:
                events.append(ev("GUILLOTINE", f"{n} {fmt_px(val, market)} 위 {dd:.2f}ATR", n, val, extra=1))

    # ---- ③ 안 좋은 조짐 (이탈 경고가 있으면 생략)
    if breakdown is None:
        why = []
        if chg <= p["big_red_pct"] and c < o and vx >= vm:
            why.append(f"장대음봉 {chg:.1f}%")
        if e50 is not None and e50p is not None and pc >= e50p and c < e50 and vx >= p["ema50_break_vol_mult"]:
            why.append(f"50EMA {fmt_px(e50, market)} 이탈")
        if hN is not None and h > hN and c <= hN and vx >= p["failed_high_vol_mult"]:
            why.append(f"{lb}일 고점 {fmt_px(hN, market)} 돌파 실패")
        if why:
            events.append(ev("WARNING", " · ".join(why) + f" · {vtxt}", extra=1))

    # ---- 돌파 후보 진행 (종가 n거래일 유지 시 확정, 그 전에 하회하면 무산)
    bull = None
    cand = bst.get(sym)
    if cand:
        try:
            k0 = ind["dates"].index(date.fromisoformat(cand["day1"]))
        except ValueError:
            k0 = None
        if k0 is None:
            bst.pop(sym, None)
        else:
            closes = ind["c"][k0:i + 1]
            if all(x > cand["level"] for x in closes):
                if len(closes) >= p["confirm_days"]:
                    bull = ev("BREAKOUT_CONFIRMED",
                              f"{cand['level_name']} {fmt_px(cand['level'], market)} 위 종가 {len(closes)}일 유지",
                              cand["level_name"], cand["level"], extra=3)
                    bst.pop(sym, None)
            else:
                events.append(ev("BREAKOUT_FAILED",
                                 f"{cand['level_name']} {fmt_px(cand['level'], market)} 재하회 ({cand['day1']} 후보)",
                                 cand["level_name"], cand["level"]))
                bst.pop(sym, None)
                cand = None
    # ---- 새 돌파 후보
    if bull is None and sym not in bst:
        res = []
        if hN is not None:
            res.append((f"{lb}일 고점", hN))
        for lv in levels:
            if lv.get("kind") == "resistance":
                res.append((lv.get("name", "저항선"), float(lv["price"])))
        broke = [(n, val) for n, val in res if c > val and pc <= val]
        if broke and vx >= vm:
            n, val = max(broke, key=lambda x: x[1])
            bst[sym] = {"level_name": n, "level": round(val, 4), "day1": d.isoformat()}
            bull = ev("BREAKOUT_CANDIDATE", f"{n} {fmt_px(val, market)} 돌파 1일차 · {vtxt}", n, val, extra=2)
    # ---- 이평선 터치 후 반등
    touched = []
    for n, m in (("20EMA", e20), ("50EMA", e50), ("200SMA", s200)):
        if m is not None and l <= m + p["touch_atr"] * a and c > m:
            touched.append((n, m))
    if touched and c > o and vx >= vm:
        txt = "·".join(n for n, _ in touched)
        if bull is None:
            n, m = touched[-1]
            bull = ev("BOUNCE", f"{txt} 터치 후 반등 양봉 · {vtxt}", n, m, extra=len(touched) * 0.5)
        else:
            bull["reason"] += f" · {txt} 터치 반등"
    if bull:
        events.append(bull)

    # ---- ④ 갭 (실적 여부는 P1에서 확인하지 않음)
    gap = (o / pc - 1) * 100
    if abs(gap) >= p["gap_pct"] and vx >= p["gap_vol_mult"]:
        events.append(ev("GAP", f"갭{'상승' if gap > 0 else '하락'} {gap:+.1f}% · {vtxt} (실적 여부 미확인)",
                         extra=1))
    return events


# ============================================================== cooldown
def apply_cooldown(events: list[dict], st: dict, p: dict, bench_dates: list[date], today: date) -> list[dict]:
    """같은 종목·같은 계열(위험/강세/갭/테마)에서 5거래일 안에 같거나 더 강한 알림이 있었으면 숨긴다.
    상태가 올라가면(예: 단두대→이탈, 후보→확정) 다시 알린다."""
    alerts = st.setdefault("alerts", {})
    kept = []
    for e in events:
        t = TYPES[e["type"]]
        if t["fam"] == "info":
            kept.append(e)
            continue
        key = f"{e['symbol']}|{t['fam']}" + (f"|{e.get('direction', '')}" if t["fam"] == "theme" else "")
        last = alerts.get(key)
        if (last and sessions_between(bench_dates, last["session"], today) < p["cooldown_sessions"]
                and last["sev"] >= t["sev"] and e["type"] != "GUILLOTINE"):
            continue
        alerts[key] = {"type": e["type"], "sev": t["sev"], "session": e["session"]}
        kept.append(e)
    return kept


def theme_events(all_events: list[dict], universe: dict, p: dict, session: str) -> list[dict]:
    members: dict[str, list[str]] = {}
    for m in universe.values():
        if m["theme"]:
            members.setdefault(m["theme"], []).append(m["symbol"])
    out = []
    for theme, syms in members.items():
        if len(syms) < p["theme_min"]:
            continue
        for direction, types, word in (("up", BULL_TYPES, "동반 강세"), ("down", BEAR_TYPES, "동반 약세")):
            hit = sorted({e["symbol"] for e in all_events if e["theme"] == theme and e["type"] in types})
            if len(hit) >= p["theme_min"]:
                names = ", ".join(universe[s]["name"] for s in hit)
                out.append({"symbol": theme, "name": theme, "tier": "index", "theme": theme,
                            "session": session, "type": "THEME", "direction": direction,
                            "reason": f"{word} {len(hit)}/{len(syms)}: {names}",
                            "score": float(len(hit)), "close": None, "chg_pct": None,
                            "level_name": None, "level": None, "atr": None, "vol_x": None})
    return out


def sort_key(e):
    t = TYPES[e["type"]]
    return (t["cat"], t["sub"], TIER_RANK.get(e["tier"], 3), -(e.get("score") or 0))


# ============================================================== text
def event_line(e: dict, market: str) -> str:
    lab = TYPES[e["type"]]["label"]
    if e["type"] == "THEME":
        return f"[{lab}] {e['reason']}"
    px = fmt_px(e["close"], market)
    return f"[{lab}] {e['name']} {px} ({e['chg_pct']:+.1f}%) · {e['reason']} · {TIER_LABEL[e['tier']]}"


def core_summary(universe: dict, events: list[dict], st: dict) -> str:
    core = [s for s, m in universe.items() if m["tier"] == "core"]
    with_ev = {e["symbol"] for e in events if e["type"] != "THEME"}
    g = [universe[s]["name"] for s in core if s in st.get("guillotine", {})]
    br = [universe[s]["name"] for s in core if any(e["symbol"] == s and e["type"] == "BREAKDOWN" for e in events)]
    wa = [universe[s]["name"] for s in core if any(e["symbol"] == s and e["type"] == "WARNING" for e in events)]
    normal = sum(1 for s in core if s not in with_ev and s not in st.get("guillotine", {}))
    parts = [f"Core 이상없음 {normal}/{len(core)}",
             "단두대: " + (", ".join(g) if g else "없음"),
             "이탈: " + (", ".join(br) if br else "없음")]
    if wa:
        parts.append("조짐: " + ", ".join(wa))
    return " / ".join(parts)


def fit_lines(head: list[str], lines: list[str], budget: int) -> list[str]:
    out = list(head)
    used = sum(len((x + "\n").encode()) for x in out)
    for k, ln in enumerate(lines):
        rest = len(lines) - k
        tail = f"… 외 {rest}건 (signals.jsonl)"
        need = len((ln + "\n").encode())
        reserve = len((tail + "\n").encode()) if rest > 1 else 0
        if used + need + reserve > budget:
            out.append(tail)
            return out
        out.append(ln)
        used += need
    return out


def render_section(market: str, mcfg: dict, session: str, now_utc: datetime, universe: dict,
                   events: list[dict], st: dict, failed: dict, stale: list[str], budget: int) -> str:
    gen = now_utc.astimezone(KST).strftime("%m/%d %H:%M")
    head = [f"## {market} {session} 종가 · 생성 {gen} KST (마감 후 배치, 실시간 아님)",
            core_summary(universe, events, st)]
    wg = [universe[s]["name"] for s, m in universe.items()
          if m["tier"] == "watch" and s in st.get("guillotine", {})]
    if wg:
        head.append("Watch 단두대: " + ", ".join(wg))
    main = [e for e in events if TYPES[e["type"]]["fam"] != "info"]
    lines = [f"{k}) {event_line(e, market)}" for k, e in enumerate(main, 1)]
    if not lines:
        lines = ["신규 시그널 없음"]
    failed_ev = [e for e in events if e["type"] == "BREAKOUT_FAILED"]
    tail = []
    if failed_ev:
        tail.append("돌파 무산: " + ", ".join(e["name"] for e in failed_ev))
    miss = [f"{s}({c})" for s, c in failed.items()] + [f"{s}(지연)" for s in stale]
    if miss:
        tail.append("데이터 미확보: " + ", ".join(miss[:8]) + (f" 외 {len(miss) - 8}" if len(miss) > 8 else ""))
    tail_bytes = sum(len((x + "\n").encode()) for x in tail)
    body = fit_lines(head, lines, budget - tail_bytes)
    return "\n".join(body + tail) + "\n"


def render_digest(parts: dict, max_bytes: int) -> str:
    head = "# 룰루루 digest (AI는 이 파일만 읽는다)\n"
    foot = "\n채권·옵션·뉴스: 미확보 (P2 예정)\n"
    body = "".join(parts.get(m, f"## {m}\n아직 실행 기록 없음\n") for m in ("US", "KR"))
    text = head + body + foot
    while len(text.encode()) > max_bytes and body:
        body = body[:-1]
        text = head + body + "\n…(잘림)\n" + foot
    return text


def render_brief(market: str, session: str, events: list[dict], n: int) -> str:
    slot = "0700" if market == "US" else "1900"
    main = [e for e in events if TYPES[e["type"]]["fam"] != "info"][:n]
    lines = [f"룰루루 {market} {session} 종가 기준 시그널 ({'06:30' if market == 'US' else '18:10'} 배치) — {slot} 브리핑용"]
    lines += [f"- {event_line(e, market)}" for e in main] or ["- 신규 시그널 없음"]
    return "\n".join(lines) + "\n"


def telegram_text(market: str, mcfg: dict, session: str, events: list[dict], n: int, digest_url: str) -> str:
    main = [e for e in events if TYPES[e["type"]]["fam"] != "info"]
    when = "06:30" if market == "US" else "18:10"
    lines = [f"룰루루 {mcfg.get('label', market)} {session} 종가 · {when} 배치(실시간 아님)"]
    lines += [event_line(e, market) for e in main[:n]]
    if len(main) > n:
        lines.append(f"… 외 {len(main) - n}건")
    if digest_url:
        lines.append(digest_url)
    return "\n".join(lines)[:4000]


# ============================================================== push
def push(text: str) -> str:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    status = "skipped_no_secret"
    if token and chat:
        try:
            data = urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
            with urlopen(Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=15) as r:
                ok = json.loads(r.read()).get("ok")
            status = "telegram_ok" if ok else "telegram_failed"
        except Exception as exc:  # 토큰이 로그에 찍히지 않도록 예외 종류만 남긴다
            status = f"telegram_failed:{type(exc).__name__}"
    topic = os.environ.get("NTFY_TOPIC")
    if status != "telegram_ok" and topic:  # 예비 채널
        try:
            with urlopen(Request(f"https://ntfy.sh/{quote(topic)}", data=text.encode()), timeout=15):
                status += "+ntfy_ok"
        except Exception as exc:
            status += f"+ntfy_failed:{type(exc).__name__}"
    return status


# ============================================================== scorecard
def scorecard(ledger_path: str, market: str, inds: dict, bench_ind: dict, p: dict) -> dict:
    stats: dict = {}
    if not os.path.exists(ledger_path):
        return stats
    bdates = bench_ind["dates"]
    bclose = dict(zip(bdates, bench_ind["c"]))
    with open(ledger_path, encoding="utf-8") as fh:
        rows = [json.loads(x) for x in fh if x.strip()]
    for r in rows:
        if r.get("market") != market or r["type"] in ("THEME", "BREAKOUT_FAILED"):
            continue
        ind = inds.get(r["symbol"])
        if not ind:
            continue
        d0 = date.fromisoformat(r["session"])
        if d0 not in ind["dates"] or d0 not in bclose:
            continue
        k0 = ind["dates"].index(d0)
        typ = r["type"]
        bucket = stats.setdefault(typ, {})
        if typ == "GUILLOTINE":
            n = p["guillotine_eval_sessions"]
            if k0 + n >= len(ind["c"]) or r.get("level") is None:
                continue
            b = bucket.setdefault(f"breach_{n}d", {"n": 0, "hit": 0})
            b["n"] += 1
            b["hit"] += any(x < r["level"] for x in ind["c"][k0 + 1:k0 + n + 1])
            continue
        if typ == "GAP":
            direction = 1 if "갭상승" in r.get("reason", "") else -1
        else:
            direction = 1 if typ in BULL_TYPES else -1
        for hz in p["score_horizons"]:
            if k0 + hz >= len(ind["c"]):
                continue
            d1 = ind["dates"][k0 + hz]
            if d1 not in bclose:
                continue
            ex = (ind["c"][k0 + hz] / ind["c"][k0] - bclose[d1] / bclose[d0]) * 100
            b = bucket.setdefault(f"h{hz}", {"n": 0, "hit": 0, "sum_excess": 0.0})
            b["n"] += 1
            b["hit"] += (ex * direction) > 0
            b["sum_excess"] += ex
    for typ, bk in stats.items():
        for k, b in bk.items():
            b["hit_rate"] = round(b["hit"] / b["n"], 3) if b["n"] else None
            if "sum_excess" in b:
                b["avg_excess_pct"] = round(b.pop("sum_excess") / b["n"], 2) if b["n"] else None
            b["note"] = "표본 부족(n<30)" if b["n"] < 30 else ""
    return stats


# ============================================================== io helpers
def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def append_jsonl(path, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ============================================================== run
def run(market: str, cfg: dict, out: str, now_utc: datetime | None = None, fetcher=yahoo_daily,
        sleep=time.sleep, dry_run: bool = False, do_push: bool = True, digest_url: str = "") -> dict:
    now_utc = now_utc or datetime.now(timezone.utc)
    p = params_of(cfg)
    mcfg = cfg["markets"][market]
    universe = build_universe(cfg, market)
    bench = mcfg["benchmark"]
    symbols = [bench] + [s for s in universe if s != bench]
    run_id = now_utc.strftime("%Y%m%dT%H%M%SZ") + "-" + market
    if os.environ.get("GITHUB_RUN_ID"):
        run_id += "-gh" + os.environ["GITHUB_RUN_ID"]

    state_path = os.path.join(out, f"state_{market.lower()}.json")
    st = read_json(state_path, {})
    log = {"run_id": run_id, "market": market, "started_kst": now_utc.astimezone(KST).isoformat(timespec="seconds"),
           "expected": len(symbols)}

    data, failed = fetch_all(symbols, p["history_range"], mcfg, now_utc, fetcher=fetcher, sleep=sleep)
    log.update(fetched=len(data), failed=len(failed))
    if bench not in data:
        log["status"] = "benchmark_missing"
        print(f"[{market}] 벤치마크 {bench} 미확보 → 판정 중단", file=sys.stderr)
        if not dry_run:
            append_jsonl(os.path.join(out, "run_log.jsonl"), [log])
        return log

    bench_ind = indicators(data[bench]["bars"], p)
    session = bench_ind["dates"][-1]
    log["session"] = session.isoformat()
    if st.get("last_session") == session.isoformat():
        log["status"] = "no_new_session"
        print(f"[{market}] {session} 이미 처리됨(휴장 또는 재실행) → 변경 없음")
        if not dry_run:
            append_jsonl(os.path.join(out, "run_log.jsonl"), [log])
        return log

    first_run = "last_session" not in st  # 첫 회차: 단두대는 현재 상태만 기록하고 알림하지 않는다
    log["first_run"] = first_run
    inds, stale, events = {}, [], []
    for sym, mem in universe.items():
        if sym not in data:
            continue
        ind = indicators(data[sym]["bars"], p)
        inds[sym] = ind
        if ind["dates"][-1] != session:
            stale.append(sym)
            continue
        evs = evaluate(sym, mem, ind, p, cfg.get("levels", {}).get(sym, []), st, market, bench_ind["dates"])
        if mem["tier"] == "index":  # 지수 상위 층은 index_types에 적힌 유형만 (소음 조절용)
            evs = [e for e in evs if e["type"] in p["index_types"] or e["type"] == "BREAKOUT_FAILED"]
        events += evs
    if first_run:
        events = [e for e in events if e["type"] != "GUILLOTINE"]
    events += theme_events(events, universe, p, session.isoformat())
    events = apply_cooldown(events, st, p, bench_ind["dates"], session)
    events.sort(key=sort_key)
    for e in events:
        e["market"] = market
        e["run_id"] = run_id
    fails = {s: c for s, c in failed.items() if s != bench}

    parts = read_json(os.path.join(out, "digest_parts.json"), {})
    budget = (p["digest_max_bytes"] - 160) // 2
    parts[market] = render_section(market, mcfg, session.isoformat(), now_utc, universe, events, st, fails, stale, budget)
    digest = render_digest(parts, p["digest_max_bytes"])
    brief = render_brief(market, session.isoformat(), events, p["brief_max"])
    pushable = [e for e in events if TYPES[e["type"]]["fam"] != "info"]
    text = telegram_text(market, mcfg, session.isoformat(), events, p["push_max"], digest_url)

    log.update(events=len(pushable), stale=len(stale), digest_bytes=len(digest.encode()))
    if dry_run:
        log["status"] = "dry_run"
        print(digest)
        print("---- brief\n" + brief)
        print("---- telegram\n" + (text if pushable else "(시그널 없음 → 발송 안 함)"))
        return log

    push_status = "no_signal" if do_push else "push_disabled"
    if pushable and do_push:
        push_status = push(text)
    for e in pushable[:p["push_max"]]:
        e["pushed"] = push_status.startswith("telegram_ok") or "ntfy_ok" in push_status
    st["last_session"] = session.isoformat()

    write_text(os.path.join(out, "digest_parts.json"), json.dumps(parts, ensure_ascii=False, indent=1))
    write_text(os.path.join(out, "digest.md"), digest)
    write_text(os.path.join(out, f"brief_{'0700' if market == 'US' else '1900'}.md"), brief)
    write_text(state_path, json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True))
    ledger = os.path.join(out, "signals.jsonl")
    append_jsonl(ledger, events)
    sc_path = os.path.join(out, "scorecard.json")
    sc = read_json(sc_path, {})
    sc[market] = {"as_of_session": session.isoformat(), "by_type": scorecard(ledger, market, inds, bench_ind, p)}
    write_text(sc_path, json.dumps(sc, ensure_ascii=False, indent=1, sort_keys=True))
    log.update(status="ok", push=push_status)
    append_jsonl(os.path.join(out, "run_log.jsonl"), [log])
    print(f"[{market}] session={session} fetched={len(data)}/{len(symbols)} failed={len(failed)} "
          f"stale={len(stale)} events={len(pushable)} push={push_status} digest={log['digest_bytes']}B")
    return log


# ============================================================== selftest
def _synthetic(n=260, start=100.0, drift=0.15, seed=1):
    """완만한 상승 + 결정적 잡음 일봉."""
    bars, d, px, x = [], date(2025, 6, 2), start, seed
    while len(bars) < n:
        if d.weekday() < 5:
            x = (x * 1103515245 + 12345) % (2 ** 31)
            noise = ((x / 2 ** 31) - 0.5) * 1.2
            o = px
            c = px + drift + noise
            h, l = max(o, c) + 0.6, min(o, c) - 0.6
            bars.append([d, o, h, l, c, 1_000_000.0])
            px = c
        d += timedelta(days=1)
    return bars


def _next_day(d):
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def selftest() -> int:
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)

    p = dict(DEFAULT_PARAMS)
    # 지표
    check(sma([1, 2, 3, 4], 2)[-1] == 3.5, "sma")
    check(abs(ema([1.0] * 30, 20)[-1] - 1.0) < 1e-9, "ema")
    check(atr([2] * 20, [1] * 20, [1.5] * 20)[-1] == 1.0, "atr")

    mem = {"name": "T", "tier": "core", "theme": "X"}

    def ev_types(bars, st=None, levels=None, tier="core"):
        ind = indicators([tuple(b) for b in bars], p)
        st = st if st is not None else {}
        m = dict(mem, tier=tier)
        return [e["type"] for e in evaluate("T", m, ind, p, levels or [], st, "US", ind["dates"])], st, ind

    base = _synthetic()
    # 돌파 후보 → 확정
    b = [list(x) for x in base]
    hi = max(x[2] for x in b[-56:-1])
    last = b[-1]
    last[1], last[4], last[2], last[3], last[5] = hi - 0.5, hi + 2, hi + 2.5, hi - 0.8, 3_000_000
    t, st, _ = ev_types(b)
    check("BREAKOUT_CANDIDATE" in t, f"breakout candidate: {t}")
    d = b[-1][0]
    for k in range(2):
        d = _next_day(d)
        px = b[-1][4] + 0.5
        b.append([d, px - 0.2, px + 0.6, px - 0.5, px, 1_000_000.0])
        t, st, _ = ev_types(b, st)
    check("BREAKOUT_CONFIRMED" in t, f"breakout confirmed day3: {t}")

    # 돌파 무산
    b2 = [list(x) for x in base]
    last = b2[-1]
    last[1], last[4], last[2], last[3], last[5] = hi - 0.5, hi + 2, hi + 2.5, hi - 0.8, 3_000_000
    t, st2, _ = ev_types(b2)
    d = _next_day(b2[-1][0])
    b2.append([d, hi, hi + 0.2, hi - 3, hi - 2.5, 1_000_000.0])
    t, st2, _ = ev_types(b2, st2)
    check("BREAKOUT_FAILED" in t, f"breakout failed: {t}")

    # 장대음봉 조짐
    b3 = [list(x) for x in base]
    pc = b3[-2][4]
    b3[-1] = [b3[-1][0], pc, pc + 0.2, pc * 0.94, pc * 0.95, 2_500_000.0]
    t, _, _ = ev_types(b3)
    check("WARNING" in t, f"warning big red: {t}")

    # 갭
    b4 = [list(x) for x in base]
    pc = b4[-2][4]
    b4[-1] = [b4[-1][0], pc * 1.06, pc * 1.08, pc * 1.05, pc * 1.07, 3_000_000.0]
    t, _, _ = ev_types(b4)
    check("GAP" in t, f"gap: {t}")

    # 단두대 진입(1회) → 유지(조용) → 이탈 승격 (넥라인 지지 사용)
    b5 = [list(x) for x in base]
    lastc = b5[-1][4]
    lv = [{"name": "넥라인", "price": lastc - 0.3, "kind": "support"}]
    t, st5, ind5 = ev_types(b5, {}, lv)
    check("GUILLOTINE" in t, f"guillotine entry: {t}")
    d = _next_day(b5[-1][0])
    b5.append([d, lastc, lastc + 0.3, lastc - 0.25, lastc + 0.05, 1_000_000.0])
    t, st5, _ = ev_types(b5, st5, lv)
    check("GUILLOTINE" not in t, f"guillotine not repeated: {t}")
    d = _next_day(d)
    b5.append([d, lastc, lastc + 0.1, lastc - 2, lastc - 1.5, 900_000.0])
    t, st5, _ = ev_types(b5, st5, lv)
    check("BREAKDOWN" in t and "T" not in st5.get("guillotine", {}), f"guillotine→breakdown: {t}")
    t2, _, _ = ev_types(b5, {}, lv, tier="index")
    check("GUILLOTINE" not in t2, "index tier has no guillotine")

    # 반등 (20EMA 터치)
    b6 = [list(x) for x in base]
    ind = indicators([tuple(x) for x in b6[:-1]], p)
    e20 = ind["ema20"][-1]
    b6[-1] = [b6[-1][0], e20 + 0.1, e20 + 1.5, e20 - 0.1, e20 + 1.2, 2_000_000.0]
    t, _, _ = ev_types(b6)
    check("BOUNCE" in t or "BREAKOUT_CANDIDATE" in t, f"bounce: {t}")

    # 재알림 억제
    stc, bd = {}, [date(2026, 10, 1) + timedelta(days=k) for k in range(10)]
    e1 = {"symbol": "A", "type": "WARNING", "session": "2026-10-02"}
    check(len(apply_cooldown([e1], stc, p, bd, date(2026, 10, 2))) == 1, "cooldown first")
    e2 = dict(e1, session="2026-10-04")
    check(len(apply_cooldown([e2], stc, p, bd, date(2026, 10, 4))) == 0, "cooldown suppress")
    e3 = dict(e1, type="BREAKDOWN", session="2026-10-05")
    check(len(apply_cooldown([e3], stc, p, bd, date(2026, 10, 5))) == 1, "cooldown upgrade")

    # 2KB 상한
    many = [{"symbol": f"S{k}", "name": f"S{k}", "tier": "watch", "theme": "", "type": "WARNING",
             "reason": "장대음봉 -4.0% · 50EMA 123.45 이탈 · 거래량 2.0×", "close": 123.45, "chg_pct": -4.0,
             "score": 1} for k in range(60)]
    uni = {f"S{k}": {"symbol": f"S{k}", "name": f"S{k}", "tier": "watch", "theme": ""} for k in range(60)}
    sec = render_section("US", {}, "2026-10-29", datetime(2026, 10, 29, 21, 30, tzinfo=timezone.utc),
                         uni, many, {}, {}, [], (p["digest_max_bytes"] - 160) // 2)
    dg = render_digest({"US": sec, "KR": sec}, p["digest_max_bytes"])
    check(len(dg.encode()) <= p["digest_max_bytes"], f"digest size {len(dg.encode())}")
    check("외" in sec, "digest truncation marker")

    # 미완성 봉 제거
    dd = {"bars": [(date(2026, 10, 29), 1, 1, 1, 1, 1)], "gmtoffset": -4 * 3600}
    r = drop_partial_bar(dd, "16:00", 20, datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc))  # 15:00 ET
    check(not r["bars"], "partial bar dropped")
    r = drop_partial_bar(dd, "16:00", 20, datetime(2026, 10, 29, 21, 30, tzinfo=timezone.utc))  # 17:30 ET
    check(len(r["bars"]) == 1, "complete bar kept")

    # 전체 실행 (가짜 Yahoo) — 같은 세션 재실행 시 변경 없음
    cfg = {"markets": {"US": {"benchmark": "SPY", "close_local": "16:00", "settle_minutes": 20, "label": "미장"}},
           "tiers": {"core": [{"symbol": "AAA", "market": "US", "theme": "X"}],
                     "watch": [{"symbol": "BBB", "market": "US", "theme": "X"}],
                     "index": [{"symbol": "AAA", "market": "US", "theme": "Y"}]},
           "levels": {}}
    check(build_universe(cfg, "US")["AAA"]["tier"] == "core", "tier priority")

    def fake(sym, rng):
        bars = [tuple(x) for x in _synthetic(seed=len(sym) + ord(sym[0]))]
        return {"bars": bars, "gmtoffset": -4 * 3600, "currency": "USD"}

    with tempfile.TemporaryDirectory() as tmp:
        now = datetime(2026, 6, 30, 21, 30, tzinfo=timezone.utc)
        l1 = run("US", cfg, tmp, now, fetcher=fake, sleep=lambda s: None, do_push=False)
        l2 = run("US", cfg, tmp, now, fetcher=fake, sleep=lambda s: None, do_push=False)
        check(l1.get("status") == "ok", f"run1 {l1}")
        check(l2.get("status") == "no_new_session", f"run2 {l2}")
        for f in ("digest.md", "brief_0700.md", "state_us.json", "scorecard.json", "run_log.jsonl"):
            check(os.path.exists(os.path.join(tmp, f)), f"missing {f}")

    if errors:
        print("SELFTEST FAIL")
        for e in errors:
            print(" -", e)
        return 1
    print("SELFTEST OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="룰루루 시그널 스크리너")
    ap.add_argument("--market", choices=["US", "KR"])
    ap.add_argument("--config", default="config/universe.json")
    ap.add_argument("--out", default="market/signals")
    ap.add_argument("--dry-run", action="store_true", help="저장·발송 없이 결과만 출력")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--test-push", action="store_true", help="텔레그램 연결 확인용 메시지 1건만 보내고 끝")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.test_push:
        st = push("룰루루 연결 테스트 — 이 메시지가 보이면 Telegram 설정 정상")
        print("push:", st)
        return 0 if "ok" in st else 1
    if not a.market:
        ap.error("--market 필요")
    cfg = load_config(a.config)
    url = ""
    if os.environ.get("GITHUB_REPOSITORY"):
        url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/blob/main/{a.out}/digest.md"
    log = run(a.market, cfg, a.out, dry_run=a.dry_run, do_push=not a.no_push, digest_url=url)
    return 0 if log.get("status") in ("ok", "no_new_session", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
