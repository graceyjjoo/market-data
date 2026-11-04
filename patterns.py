#!/usr/bin/env python3
"""룰루루 P3: 자동 패턴 스캔 + 자동 차트 명세(spec_auto).

- 패턴 이름·번호는 SNUME 패턴 카탈로그(29항목)를 따른다. 컵앤핸들은 카탈로그 밖이라 번호 없이 쓴다.
- 앵커는 실제 고저점(피벗)만 쓴다. 미관측 구간(미래)은 그리지 않는다.
- 자동 스캔은 '후보'다. 판단은 Claude가 analysis.json 카드로 확정한다(채택 시 ad=true).
- 출력 도형 형식은 기존 룰루루 아티팩트의 SPEC.ov 형식과 같다
  (zones, hlines, lines, arcs, boxes, polys, dots, texts, fib, event).
"""
from __future__ import annotations

from datetime import date

CATALOG = {
    "01": "01 지지·저항", "02": "02 추세선", "03": "03 상승·하락 채널", "04": "04 박스·횡보",
    "05": "05 상승삼각형", "06": "06 하락삼각형", "07": "07 대칭삼각수렴", "08": "08 상승쐐기",
    "09": "09 하락쐐기", "10": "10 이중바닥", "11": "11 이중천장", "12": "12 삼중바닥", "13": "13 삼중천장",
    "14": "14 헤드앤숄더", "15": "15 역헤드앤숄더", "16": "16 불 플래그", "17": "17 베어 플래그",
    "18": "18 페넌트", "19": "19 VCP·변동성 수축", "20": "20 라운딩 바닥", "21": "21 라운딩 천장",
    "22": "22 이평선 지지·회복", "23": "23 돌파·재시험·실패", "24": "24 갭 구간",
    "25": "25 강세·약세 다이버전스", "26": "26 Squeeze·변동성 압축", "27": "27 Stage·추세 단계",
    "28": "28 Anchored VWAP", "29": "29 Fibonacci 참조선",
}
SCORE_MIN = 0.6


def _r(x):
    if x is None:
        return None
    return round(x, 4) if abs(x) < 10 else round(x, 2)


# ============================================================== pivots
def pivots(h, l, atr, k=5, min_swing_atr=1.2, lookback=260):
    """프랙탈(좌우 k봉) 고저점 → 고·저 번갈아 정리 → ATR 기준 작은 흔들림 제거."""
    n = len(h)
    s = max(k, n - lookback)
    raw = []
    for i in range(s, n - k):
        if h[i] == max(h[i - k:i + k + 1]):
            raw.append([i, h[i], "H"])
        if l[i] == min(l[i - k:i + k + 1]):
            raw.append([i, l[i], "L"])
    raw.sort(key=lambda x: (x[0], x[2] == "L"))
    out = []
    for p in raw:
        if out and out[-1][2] == p[2]:
            if (p[2] == "H" and p[1] >= out[-1][1]) or (p[2] == "L" and p[1] <= out[-1][1]):
                out[-1] = p
            continue
        out.append(p)
    changed = True
    while changed and len(out) > 2:  # 작은 스윙 제거
        changed = False
        for j in range(1, len(out)):
            a = atr[out[j][0]] or atr[-1]
            if abs(out[j][1] - out[j - 1][1]) < min_swing_atr * a:
                drop = j if j == len(out) - 1 else (j - 1 if j > 1 else j)
                out.pop(drop)
                k2 = 1
                while k2 < len(out):  # 다시 번갈아 정리
                    if out[k2][2] == out[k2 - 1][2]:
                        keep_hi = out[k2][2] == "H"
                        worse = k2 - 1 if ((out[k2][1] >= out[k2 - 1][1]) == keep_hi) else k2
                        out.pop(worse)
                    else:
                        k2 += 1
                changed = True
                break
    return out


def _line_at(a, b, i):
    (i1, p1), (i2, p2) = a, b
    return p1 + (p2 - p1) / (i2 - i1) * (i - i1) if i2 != i1 else p1


# ============================================================== detectors
def double_patterns(P, c, atr, dates, n):
    out = []
    A = atr[-1]
    for j in range(len(P) - 2):
        a, m, b = P[j], P[j + 1], P[j + 2]
        if b[0] < n - 70 or a[2] == m[2]:
            continue
        bottom = a[2] == "L"
        tol = 1.5 * A
        diff = abs(a[1] - b[1])
        depth = (m[1] - max(a[1], b[1])) if bottom else (min(a[1], b[1]) - m[1])
        sep = b[0] - a[0]
        if diff > tol or depth < 2 * A or not (10 <= sep <= 130):
            continue
        neck = m[1]
        last = c[-1]
        if bottom:
            st = "넥라인 돌파" if last > neck else "넥라인 아래"
            tgt = neck + (neck - (a[1] + b[1]) / 2)
        else:
            st = "넥라인 이탈" if last < neck else "넥라인 위"
            tgt = neck - ((a[1] + b[1]) / 2 - neck)
        score = round(0.4 * (1 - diff / tol) + 0.3 * min(depth / (4 * A), 1) + 0.3 * (1 if b[0] >= n - 40 else 0.5), 2)
        code = "10" if bottom else "11"
        lab = ("B1", "B2") if bottom else ("T1", "T2")
        ov = {"polys": [{"pts": [[dates[a[0]], _r(a[1])], [dates[m[0]], _r(m[1])], [dates[b[0]], _r(b[1])]], "g": "aux1"}],
              "hlines": [{"p": _r(neck), "label": f"Neckline {_r(neck)}", "g": "aux1", "from": dates[a[0]]},
                         {"p": _r(tgt), "label": f"측정 목표 {_r(tgt)}", "g": "aux1", "from": dates[b[0]]}],
              "texts": [{"d": dates[a[0]], "p": _r(a[1]), "dy": 18 if bottom else -18, "t": lab[0], "g": "aux1", "al": "center"},
                        {"d": dates[b[0]], "p": _r(b[1]), "dy": 18 if bottom else -18, "t": lab[1], "g": "aux1", "al": "center"}]}
        out.append({"code": code, "kind": CATALOG[code][3:], "st": st, "score": score, "ov": ov,
                    "anchors": [[dates[x[0]], _r(x[1])] for x in (a, m, b)], "neck": _r(neck), "target": _r(tgt)})
    return out


def head_shoulders(P, c, atr, dates, n):
    out = []
    A = atr[-1]
    for j in range(len(P) - 4):
        s1, t1, hd, t2, s2 = P[j:j + 5]
        if s2[0] < n - 70:
            continue
        top = s1[2] == "H"
        if top and not (hd[1] > max(s1[1], s2[1]) + A):
            continue
        if not top and not (hd[1] < min(s1[1], s2[1]) - A):
            continue
        sh_diff = abs(s1[1] - s2[1])
        if sh_diff > 2.5 * A:
            continue
        neck_end = _line_at((t1[0], t1[1]), (t2[0], t2[1]), n - 1)
        if abs(t2[1] - t1[1]) > 3 * A:
            continue
        last = c[-1]
        head_h = abs(hd[1] - _line_at((t1[0], t1[1]), (t2[0], t2[1]), hd[0]))
        if top:
            st = "넥라인 이탈" if last < neck_end else "넥라인 위"
            tgt = neck_end - head_h
        else:
            st = "넥라인 돌파" if last > neck_end else "넥라인 아래"
            tgt = neck_end + head_h
        score = round(0.35 * (1 - sh_diff / (2.5 * A)) + 0.35 * min(head_h / (5 * A), 1) +
                      0.3 * (1 - abs(t2[1] - t1[1]) / (3 * A)), 2)
        code = "14" if top else "15"
        pts = [[dates[x[0]], _r(x[1])] for x in (s1, t1, hd, t2, s2)]
        dy = -18 if top else 18
        ov = {"polys": [{"pts": pts, "g": "aux1"}],
              "lines": [{"a": pts[1], "b": pts[3], "end": "ext", "g": "aux1", "label": "넥라인"}],
              "hlines": [{"p": _r(tgt), "label": f"측정 목표 {_r(tgt)}", "g": "aux1", "from": dates[s2[0]]}],
              "texts": [{"d": pts[0][0], "p": pts[0][1], "dy": dy, "t": "LS", "g": "aux1", "al": "center"},
                        {"d": pts[2][0], "p": pts[2][1], "dy": dy, "t": "H", "g": "aux1", "al": "center"},
                        {"d": pts[4][0], "p": pts[4][1], "dy": dy, "t": "RS", "g": "aux1", "al": "center"}]}
        out.append({"code": code, "kind": CATALOG[code][3:], "st": st, "score": score, "ov": ov,
                    "anchors": pts, "neck": _r(neck_end), "target": _r(tgt)})
    return out


def cup_handle(P, h, l, c, atr, dates, n):
    out = []
    A = atr[-1]
    highs = [p for p in P if p[2] == "H" and p[0] >= n - 260]
    for x in range(len(highs)):
        for y in range(x + 1, len(highs)):
            lr, rr = highs[x], highs[y]
            span = rr[0] - lr[0]
            if span < 25 or rr[0] < n - 60:
                continue
            bi = min(range(lr[0], rr[0] + 1), key=lambda i: l[i])
            bot = l[bi]
            rim = min(lr[1], rr[1])
            depth = (rim - bot) / rim
            if not (0.12 <= depth <= 0.6):
                continue
            if abs(lr[1] - rr[1]) > max(3 * A, 0.06 * rim):
                continue
            pos = (bi - lr[0]) / span
            if not (0.2 <= pos <= 0.8):
                continue
            after = list(range(rr[0] + 1, n))
            hlow = min((l[i] for i in after), default=None)
            last = c[-1]
            top = max(lr[1], rr[1])
            ov = {"arcs": [{"l": [dates[lr[0]], _r(lr[1])], "b": [dates[bi], _r(bot)], "r": [dates[rr[0]], _r(rr[1])],
                            "g": "aux1", "label": "컵"}]}
            handle_ok = hlow is not None and len(after) >= 3 and (rr[1] - hlow) <= 0.5 * (rr[1] - bot)
            if last > top:
                st = "림 돌파"
            elif handle_ok:
                st = "핸들 형성 중"
                ov["boxes"] = [{"a": dates[rr[0]], "z": dates[n - 1], "lo": _r(hlow), "hi": _r(rr[1]), "g": "aux1",
                                "label": "핸들"}]
            else:
                st = "핸들 미형성"
            ov["hlines"] = [{"p": _r(top), "label": f"림 {_r(top)}", "g": "aux1", "from": dates[lr[0]]}]
            score = round(0.35 * (1 - abs(lr[1] - rr[1]) / max(3 * A, 0.06 * rim)) + 0.25 * (1 - abs(pos - 0.5) * 2)
                          + 0.2 * (1 if handle_ok or last > top else 0.4) + 0.2 * min(span / 60, 1), 2)
            out.append({"code": "", "kind": "컵앤핸들" if handle_ok else "컵(핸들 미형성)", "st": st, "score": score,
                        "ov": ov, "anchors": [[dates[lr[0]], _r(lr[1])], [dates[bi], _r(bot)], [dates[rr[0]], _r(rr[1])]],
                        "neck": _r(top), "target": _r(top + (top - bot))})
    return sorted(out, key=lambda r: -r["score"])[:1]


def convergence(P, h, l, c, atr, dates, n, window=110):
    A = atr[-1]
    H = [p for p in P if p[2] == "H" and p[0] >= n - window]
    L = [p for p in P if p[2] == "L" and p[0] >= n - window]
    if len(H) < 2 or len(L) < 2:
        return []
    ua, ub = H[0], H[-1]
    la, lb = L[0], L[-1]
    if ub[0] - ua[0] < 15 or lb[0] - la[0] < 15:
        return []
    up = lambda i: _line_at((ua[0], ua[1]), (ub[0], ub[1]), i)
    lo = lambda i: _line_at((la[0], la[1]), (lb[0], lb[1]), i)
    s0 = min(ua[0], la[0])
    inside = sum(1 for i in range(s0, n) if lo(i) - 0.5 * A <= c[i] <= up(i) + 0.5 * A) / max(1, n - s0)
    su = (ub[1] - ua[1]) / (ub[0] - ua[0]) / A
    sl = (lb[1] - la[1]) / (lb[0] - la[0]) / A
    flat = 0.015
    w0, w1 = up(s0) - lo(s0), up(n - 1) - lo(n - 1)
    if w1 <= 0:
        return []
    conv = w1 < 0.8 * w0
    if abs(su) < flat and sl > flat and conv:
        code = "05"
    elif su < -flat and abs(sl) < flat and conv:
        code = "06"
    elif su < -flat and sl > flat:
        code = "07"
    elif su > flat and sl > flat and conv:
        code = "08"
    elif su < -flat and sl < -flat and conv:
        code = "09"
    elif abs(su - sl) < flat and abs(su) >= flat:
        code = "03"
    else:
        return []
    last = c[-1]
    st = "상단 돌파" if last > up(n - 1) else ("하단 이탈" if last < lo(n - 1) else "내부")
    touches = len(H) + len(L)
    score = round(0.45 * inside + 0.3 * min(touches / 6, 1) + 0.25 * (1 if conv or code == "03" else 0.5), 2)
    name = CATALOG[code][3:]
    ov = {"lines": [{"a": [dates[ua[0]], _r(ua[1])], "b": [dates[ub[0]], _r(ub[1])], "end": "ext", "g": "aux1",
                     "label": "상단"},
                    {"a": [dates[la[0]], _r(la[1])], "b": [dates[lb[0]], _r(lb[1])], "end": "ext", "g": "aux1",
                     "label": "하단"}],
          "dots": [[dates[p[0]], _r(p[1]), "aux1"] for p in H + L]}
    return [{"code": code, "kind": name, "st": st, "score": score, "ov": ov,
             "anchors": [[dates[p[0]], _r(p[1])] for p in (ua, ub, la, lb)],
             "upper_now": _r(up(n - 1)), "lower_now": _r(lo(n - 1))}]


def scan(ind: dict) -> dict:
    """반환: {pivots:[...], cands:[...상위 3, score≥0.6]}"""
    h, l, c, atr, dates = ind["h"], ind["l"], ind["c"], ind["atr"], [d.isoformat() for d in ind["dates"]]
    n = len(c)
    if n < 80 or not atr[-1]:
        return {"pivots": [], "cands": []}
    atr = [a if a else atr[-1] for a in atr]
    P = pivots(h, l, atr)
    cands = (double_patterns(P, c, atr, dates, n) + head_shoulders(P, c, atr, dates, n)
             + cup_handle(P, h, l, c, atr, dates, n) + convergence(P, h, l, c, atr, dates, n))
    pos = {d: i for i, d in enumerate(dates)}
    A = atr[-1]
    for x in cands:  # 최근성·현재가 근접도 보정 (오래되고 멀어진 구조는 점수 하향)
        age = n - 1 - max(pos.get(a[0], 0) for a in x["anchors"])
        x["score"] = round(x["score"] * (1 if age <= 30 else 0.85 if age <= 60 else 0.7), 2)
        ref = x.get("neck") or x.get("upper_now")
        if ref and abs(c[-1] - ref) > 4 * A:
            x["score"] = round(x["score"] * 0.8, 2)
    best = {}
    for x in cands:  # 같은 종류는 최고점 하나만
        if x["score"] >= SCORE_MIN and (x["kind"] not in best or x["score"] > best[x["kind"]]["score"]):
            best[x["kind"]] = x
    top = sorted(best.values(), key=lambda r: -r["score"])[:3]
    piv = [[dates[p[0]], _r(p[1]), p[2]] for p in P[-12:]]
    return {"pivots": piv, "cands": top}


# ============================================================== fib / vbp / macd
def fib_auto(ind, nbars=170):
    h, l, dates = ind["h"], ind["l"], ind["dates"]
    k = max(0, len(h) - nbars)
    hi = max(range(k, len(h)), key=lambda i: h[i])
    lo = min(range(k, len(l)), key=lambda i: l[i])
    a, b = (hi, lo) if hi < lo else (lo, hi)
    pa = h[a] if a == hi else l[a]
    pb = h[b] if b == hi else l[b]
    return {"a": [dates[a].isoformat(), _r(pa)], "b": [dates[b].isoformat(), _r(pb)], "g": "fib"}


def vbp(ind, nbars=170, bins=24):
    k = max(0, len(ind["c"]) - nbars)
    lo, hi = min(ind["l"][k:]), max(ind["h"][k:])
    if hi <= lo:
        return {"edges": [lo, hi], "up": [0], "dn": [0]}
    step = (hi - lo) / bins
    up, dn = [0.0] * bins, [0.0] * bins
    for i in range(k, len(ind["c"])):
        a, b, v = ind["l"][i], ind["h"][i], ind["v"][i]
        b0, b1 = int((a - lo) / step), int((b - lo) / step)
        b0, b1 = max(0, min(bins - 1, b0)), max(0, min(bins - 1, b1))
        share = v / (b1 - b0 + 1)
        tgt = up if ind["c"][i] >= ind["o"][i] else dn
        for j in range(b0, b1 + 1):
            tgt[j] += share
    m = max(u + d for u, d in zip(up, dn)) or 1
    return {"edges": [_r(lo + j * step) for j in range(bins + 1)],
            "up": [round(u / m, 3) for u in up], "dn": [round(d / m, 3) for d in dn]}


def ema_list(vals, n):
    out = [None] * len(vals)
    a = 2 / (n + 1)
    e = None
    for i, v in enumerate(vals):
        if v is None:
            continue
        e = v if e is None else v * a + e * (1 - a)
        out[i] = e
    return out


def macd(c):
    e12, e26 = ema_list(c, 12), ema_list(c, 26)
    m = [a - b for a, b in zip(e12, e26)]
    s = ema_list(m, 9)
    return m, s


# ============================================================== spec_auto
SIG_CAT = {"BREAKDOWN": "01", "GUILLOTINE": "01", "WARNING": "23", "BREAKOUT_CONFIRMED": "23",
           "BREAKOUT_CANDIDATE": "23", "BOUNCE": "22", "GAP": "24", "BREAKOUT_FAILED": "23"}
SIG_CHIP = {"BREAKDOWN": "warn", "WARNING": "warn", "GUILLOTINE": "retest", "BREAKOUT_CONFIRMED": "ok",
            "BREAKOUT_CANDIDATE": "ok", "BOUNCE": "ok", "GAP": "watch", "BREAKOUT_FAILED": "warn"}


def fmt(x, kr):
    return "–" if x is None else (f"{x:,.0f}" if kr else f"{x:,.2f}")


def spec_auto(item: dict, ind: dict, sc: dict, kr: bool, start_date: str) -> dict:
    c, atr = ind["c"][-1], ind["atr"][-1] or 0
    e50, s200 = ind["ema50"][-1], ind["sma200"][-1]
    sigs = [s for s in item["signals"] if s["type"] != "THEME"]
    types = {s["type"] for s in sigs}
    if types & {"BREAKDOWN", "WARNING", "BREAKOUT_FAILED"}:
        sev = "dn"
    elif types & {"BREAKOUT_CONFIRMED", "BREAKOUT_CANDIDATE", "BOUNCE"}:
        sev = "up"
    else:
        sev = "watch"
    # 트리거: 위쪽은 가장 가까운 저항, 아래쪽은 가장 가까운 지지 (실제 값만)
    res, sup = [], []
    hN = max(ind["h"][-56:-1])
    res.append((hN, f"종가 {fmt(hN, kr)}(55일 고점) 상회"))
    for lv in item["levels"]:
        p = lv["price"]
        (res if p > c else sup).append((p, f"종가 {fmt(p, kr)}({lv['name']}) {'상회' if p > c else '하회'}"))
    for nm, v in (("50EMA", e50), ("200SMA", s200)):
        if v:
            (res if v > c else sup).append((v, f"종가 {fmt(v, kr)}({nm}) {'회복' if v > c else '하회'}"))
    for x in sc["cands"]:
        for key, nm in (("neck", "넥라인"), ("upper_now", "상단선"), ("lower_now", "하단선")):
            v = x.get(key)
            if v:
                (res if v > c else sup).append((v, f"종가 {fmt(v, kr)}({x['kind']} {nm}) {'상회' if v > c else '하회'}"))
    lows = [p for p in sc["pivots"] if p[2] == "L" and p[1] < c]
    if lows:
        sup.append((lows[-1][1], f"종가 {fmt(lows[-1][1], kr)}({lows[-1][0][5:].replace('-', '/')} 저점) 하회"))
    res = [r for r in res if r[0] > c] or [(c + 2 * atr, "기준 없음(+2ATR)")]
    sup = [s for s in sup if s[0] < c] or [(c - 2 * atr, "기준 없음(-2ATR)")]
    up = min(res, key=lambda r: r[0])
    dn = max(sup, key=lambda s: s[0])
    first = sigs[0] if sigs else None
    sig_txt = f"{first['label']} — {first['reason']}" if first else "새 신호 없음"
    pos = []
    rs = first["reason"] if first else ""
    if e50 and "50EMA" not in rs:
        pos.append(f"50EMA {fmt(e50, kr)} {'위' if c >= e50 else '아래'}")
    if s200 and "200SMA" not in rs:
        pos.append(f"200SMA {fmt(s200, kr)} {'위' if c >= s200 else '아래'}")
    head = (first["reason"] if first else "새 신호 없음") + " · " + " · ".join(pos)

    ov = {"fib": fib_auto(ind), "hlines": [], "lines": [], "zones": [], "dots": [], "texts": [], "polys": [],
          "arcs": [], "boxes": []}
    for lv in item["levels"]:
        if lv["kind"] in ("guillotine", "breakout", "support", "resistance"):
            ov["hlines"].append({"p": _r(lv["price"]), "label": f"{lv['name']} {fmt(lv['price'], kr)}", "g": "p1",
                                 "c": "amber", "from": start_date})
        elif lv["kind"] in ("call_wall", "put_wall", "max_pain"):
            ov["hlines"].append({"p": _r(lv["price"]), "label": f"{lv['name']} {fmt(lv['price'], kr)}", "g": "p2",
                                 "from": item["t"][-30]})
    cards = []
    for k, s in enumerate(sigs):
        code = SIG_CAT.get(s["type"], "01")
        if s["type"] in ("BREAKDOWN", "GUILLOTINE", "WARNING") and ("EMA" in s["reason"] or "SMA" in s["reason"]):
            code = "22"
        dl = [["종가", fmt(c, kr)], ["ATR(14)", fmt(atr, kr)]]
        if s.get("level") is not None:
            dl.insert(1, ["기준선", fmt(s["level"], kr)])
        cards.append({"id": "p1", "n": CATALOG[code], "chip": [SIG_CHIP.get(s["type"], "watch"), s["label"]],
                      "p": s["reason"] + (s.get("extra") or ""), "dl": dl, "fit": ""})
    if not cards:
        cards.append({"id": "p1", "n": CATALOG["22"], "chip": ["watch", "관찰"],
                      "p": " · ".join(pos) + f" · ATR(14) {fmt(atr, kr)}", "dl": [], "fit": ""})
    scan_list = [{"kind": x["kind"], "st": x["st"], "score": x["score"], "ad": False} for x in sc["cands"]]
    if sc["cands"]:  # 최고점 후보를 보조 패턴으로 표시
        x = sc["cands"][0]
        for key, vals in x["ov"].items():
            ov.setdefault(key, []).extend(vals)
        dl = [["앵커", " · ".join(f"{a[0][5:].replace('-', '/')} {fmt(a[1], kr)}" for a in x["anchors"])]]
        if x.get("neck"):
            dl.append(["기준선", fmt(x["neck"], kr)])
        if x.get("target"):
            dl.append(["측정 목표", fmt(x["target"], kr)])
        name = CATALOG.get(x["code"], x["kind"]) if x["code"] else x["kind"]
        cards.append({"id": "aux1", "aux": True, "n": name + " 후보", "chip": ["watch", x["st"]],
                      "p": f"자동 스캔 후보(점수 {x['score']:.2f})입니다. 사람 확인 전이라 형태 일치도는 비워 둡니다.",
                      "dl": dl, "fit": ""})
    news = [{"date": "", "t": n["title"], "u": n.get("link") or "", "s": f"Yahoo {n['t']}"}
            for n in (item.get("enrich") or {}).get("news", [])]
    er = (item.get("enrich") or {}).get("earnings")
    if er and er["date"] <= item["t"][-1] and er["date"] >= item["t"][0]:
        ov["event"] = {"d": er["date"], "t": f"실적 {er['date'][5:].replace('-', '/')}"}
    return {"name": item["name"], "grp": item["theme"] or item["tier"], "prev": None, "prevLabel": "",
            "status": [{"dn": "warn", "up": "ok", "watch": "watch"}[sev], first["label"] if first else "관찰"],
            "range": start_date, "head": head,
            "alert": {"sig": sig_txt, "sev": sev, "up": [_r(up[0]), up[1]], "dn": [_r(dn[0]), dn[1]]},
            "ov": ov, "cards": cards, "delta": "자동 생성 명세입니다. Claude 분석(analysis.json)이 있으면 그것이 우선합니다.",
            "scan": scan_list, "news": news, "auto": True}
