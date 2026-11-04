#!/usr/bin/env python3
"""룰루루 LIVE 갱신 도우미 (Claude 작업 공간에서 실행, 저장소에 올리지 않음).

1) 공개 저장소에서 chart_data.json · digest.md · analysis_input_*.md · market/yahoo/latest.json 을 내려받는다.
2) latest.json(기존 시간별 수집)으로 지수 띠(macro.json)를 만든다. summary 문장은 Claude가 채운다(선택).
3) 직전 게시본 chart_data.json(있으면)으로 '직전 판 대비' 기준값(prev)을 만든다.
4) Claude가 쓴 분석(--claude, 종목별 SPEC 일부/전체)을 합쳐 analysis.json을 만든다.
5) 출력 폴더 = 아티팩트 게시용: lululu_live.html + chart_data.json + analysis.json + macro.json

사용:
  python update_live.py --out DIR [--prev PREV_chart_data.json] [--claude claude_analysis.json]
                        [--repo graceyjjoo/market-data] [--html artifact/lululu_live.html]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
FILES = ["market/signals/chart_data.json", "market/signals/digest.md", "market/signals/analysis_input_us.md",
         "market/signals/analysis_input_kr.md", "market/yahoo/latest.json"]
MACRO = {
    "us": [("S&P 500", "^GSPC", 2), ("Nasdaq", "^IXIC", 2), ("Nasdaq-100", "^NDX", 2), ("SOX", "^SOX", 2),
           ("VIX", "^VIX", 2), ("미 10년물", "^TNX", 3), ("WTI", "CL=F", 2), ("원·달러", "KRW=X", 1)],
    "kr": [("KOSPI", "^KS11", 2), ("KOSDAQ", "^KQ11", 2), ("원·달러", "KRW=X", 1), ("엔·원", "JPYKRW=X", 2),
           ("나스닥100 선물", "NQ=F", 2)],
}


def fetch(repo, path, dest):
    url = f"https://raw.githubusercontent.com/{repo}/main/{path}"
    r = subprocess.run(["curl", "-sS", "-f", "-o", dest, url], capture_output=True, text=True)
    return r.returncode == 0


def macro(latest: dict) -> dict:
    ins = latest.get("instruments", {})
    out = {}
    for mk, rows in MACRO.items():
        items = []
        for lab, sym, dp in rows:
            lv = (ins.get(sym) or {}).get("last_valid")
            if not lv:
                continue
            v = lv["value"]
            val = f"{v:.{dp}f}%" if sym == "^TNX" else f"{v:,.{dp}f}"
            as_of = lv.get("as_of", "")
            try:
                note = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(KST).strftime("%m/%d %H:%M")
            except ValueError:
                note = ""
            items.append([lab, val, lv.get("day_change_pct"), note])
        out[mk] = {"items": items, "summary": "", "sources": [{"s": "기존 시간별 수집(Yahoo)", "u": ""}]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default="graceyjjoo/market-data")
    ap.add_argument("--prev")
    ap.add_argument("--claude")
    ap.add_argument("--html", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifact", "lululu_live.html"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    got = {}
    for p in FILES:
        dest = os.path.join(a.out, os.path.basename(p))
        got[p] = fetch(a.repo, p, dest)
    print("download:", {os.path.basename(k): v for k, v in got.items()})
    if not got["market/signals/chart_data.json"]:
        raise SystemExit("chart_data.json 없음 — 저장소에서 룰루루 실행(실제 실행)이 한 번도 안 된 상태")
    cd = json.load(open(os.path.join(a.out, "chart_data.json"), encoding="utf-8"))

    mac = {}
    if got["market/yahoo/latest.json"]:
        mac = macro(json.load(open(os.path.join(a.out, "latest.json"), encoding="utf-8")))
    old_mac = os.path.join(a.out, "macro_summary.json")  # Claude가 쓴 요약 문장(선택)
    if os.path.exists(old_mac):
        for mk, v in json.load(open(old_mac, encoding="utf-8")).items():
            mac.setdefault(mk, {}).update(v)
    json.dump(mac, open(os.path.join(a.out, "macro.json"), "w", encoding="utf-8"), ensure_ascii=False)

    an = {"updated_kst": datetime.now(KST).strftime("%m/%d %H:%M"), "US": {}, "KR": {}, "notes": {}, "corrections": [],
          "prev": {}}
    if a.prev and os.path.exists(a.prev):
        pv = json.load(open(a.prev, encoding="utf-8"))
        for mk, part in (pv.get("markets") or {}).items():
            ses = part.get("session", "")
            for it in part.get("items", []):
                last = it.get("last", {})
                c = last.get("c", last.get("close"))
                if c is not None:
                    an["prev"][it["symbol"]] = [c, f"{ses[5:].replace('-', '/')} 판"]
    if a.claude and os.path.exists(a.claude):
        cl = json.load(open(a.claude, encoding="utf-8"))
        for k in ("US", "KR", "notes"):
            an[k].update(cl.get(k, {}))
        an["corrections"] = cl.get("corrections", [])
    json.dump(an, open(os.path.join(a.out, "analysis.json"), "w", encoding="utf-8"), ensure_ascii=False)
    shutil.copy(a.html, os.path.join(a.out, "lululu_live.html"))
    n = {mk: len(p.get("items", [])) for mk, p in cd.get("markets", {}).items()}
    print("ready:", a.out, "items", n, "claude specs", {k: len(an[k]) for k in ("US", "KR")},
          "prev", len(an["prev"]), "sessions", {mk: p.get("session") for mk, p in cd.get("markets", {}).items()})


if __name__ == "__main__":
    main()
