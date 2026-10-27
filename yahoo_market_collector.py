#!/usr/bin/env python3
"""Yahoo Finance Chart JSON collector.

Market Data Extraction Guide 3~12절 구현.
- 한 번 실행(run)할 때 48개 티커를 순서대로 조회해 transient CSV 하나를 만든다.
- canonical store는 건드리지 않는다 (저장은 yahoo_store.py 담당).
- 표준 라이브러리만 사용한다 (pip 설치 불필요).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
KST = timezone(timedelta(hours=9))
FRESH_MAX_MINUTES = 30  # as_of가 이보다 오래되면 old_quote_or_closed

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass(frozen=True)
class Instrument:
    group: str
    name: str
    symbol: str
    kind: str
    unit: str


# ---------------------------------------------------------------- 10절 Universe (48)
INSTRUMENTS: list[Instrument] = [
    # 한국
    Instrument("korea", "KOSPI", "^KS11", "index", "index_points"),
    Instrument("korea", "KOSDAQ", "^KQ11", "index", "index_points"),
    # 미국 주요 지수
    Instrument("us_index", "S&P 500", "^GSPC", "index", "index_points"),
    Instrument("us_index", "Nasdaq 100", "^NDX", "index", "index_points"),
    Instrument("us_index", "PHLX Semiconductor Index", "^SOX", "index", "index_points"),
    Instrument("us_index", "Russell 2000", "^RUT", "index", "index_points"),
    Instrument("us_index", "Nasdaq Composite", "^IXIC", "index", "index_points"),
    # 일본 / 중화권
    Instrument("asia", "Nikkei 225", "^N225", "index", "index_points"),
    Instrument("asia", "Hang Seng", "^HSI", "index", "index_points"),
    Instrument("asia", "Hang Seng TECH", "HSTECH.HK", "index", "index_points"),
    Instrument("asia", "CSI 300", "000300.SS", "index", "index_points"),
    Instrument("asia", "Shanghai Composite", "000001.SS", "index", "index_points"),
    Instrument("asia", "Shenzhen Component", "399001.SZ", "index", "index_points"),
    Instrument("asia", "TOPIX ETF proxy (not spot index)", "1306.T", "ETF_proxy", "share_price"),
    # 미국 금리 (Yahoo yield index/proxy, 공식 국채 수익률 아님)
    Instrument("rates", "US 13-week Treasury yield (Yahoo proxy)", "^IRX", "government_yield", "percent"),
    Instrument("rates", "US 5-year Treasury yield (Yahoo proxy)", "^FVX", "government_yield", "percent"),
    Instrument("rates", "US 10-year Treasury yield (Yahoo proxy)", "^TNX", "government_yield", "percent"),
    Instrument("rates", "US 30-year Treasury yield (Yahoo proxy)", "^TYX", "government_yield", "percent"),
    # FX
    Instrument("fx", "USD/KRW", "KRW=X", "fx", "KRW_per_USD"),
    Instrument("fx", "USD/JPY", "JPY=X", "fx", "JPY_per_USD"),
    Instrument("fx", "USD/CNY", "CNY=X", "fx", "CNY_per_USD"),
    Instrument("fx", "EUR/USD", "EURUSD=X", "fx", "USD_per_EUR"),
    Instrument("fx", "JPY/KRW", "JPYKRW=X", "fx", "KRW_per_JPY"),
    # 미국 주가지수 선물
    Instrument("futures", "E-mini Nasdaq 100 futures", "NQ=F", "futures", "index_points"),
    Instrument("futures", "E-mini S&P 500 futures", "ES=F", "futures", "index_points"),
    # 원자재
    Instrument("commodities", "WTI crude oil futures", "CL=F", "futures", "USD_per_barrel"),
    Instrument("commodities", "Gold futures", "GC=F", "futures", "USD_per_troy_ounce"),
    Instrument("commodities", "Copper futures", "HG=F", "futures", "USD_per_pound"),
    Instrument("commodities", "Natural gas futures", "NG=F", "futures", "USD_per_MMBtu"),
    # 변동성
    Instrument("volatility", "CBOE VIX", "^VIX", "index", "index_points"),
    # ETF 18 (name = 테마명)
    Instrument("etf", "한국 주식", "EWY", "ETF", "share_price"),
    Instrument("etf", "반도체", "SOXX", "ETF", "share_price"),
    Instrument("etf", "중국 인터넷", "KWEB", "ETF", "share_price"),
    Instrument("etf", "금융", "XLF", "ETF", "share_price"),
    Instrument("etf", "산업재", "XLI", "ETF", "share_price"),
    Instrument("etf", "경기소비재", "XLY", "ETF", "share_price"),
    Instrument("etf", "필수소비재", "XLP", "ETF", "share_price"),
    Instrument("etf", "에너지", "XLE", "ETF", "share_price"),
    Instrument("etf", "M7(빅테크)", "MAGS", "ETF", "share_price"),
    Instrument("etf", "양자컴퓨팅", "QTUP", "ETF", "share_price"),
    Instrument("etf", "우주산업", "WARP", "ETF", "share_price"),
    Instrument("etf", "전력인프라", "JOUL", "ETF", "share_price"),
    Instrument("etf", "AI 광통신·포토닉스", "LYTE", "ETF", "share_price"),
    Instrument("etf", "원전·SMR 밸류체인", "NLR", "ETF", "share_price"),
    Instrument("etf", "바이오테크", "XBI", "ETF", "share_price"),
    Instrument("etf", "휴머노이드", "HUMN", "ETF", "share_price"),
    Instrument("etf", "구리광산", "COPX", "ETF", "share_price"),
    Instrument("etf", "사이버보안", "CIBR", "ETF", "share_price"),
]

# 9절: Yahoo가 다른 symbol로 돌려줘도 허용하는 FX alias (통화 + 상품명 둘 다 맞아야 함)
FX_ALIASES = {
    "JPY=X": {"returned": {"USDJPY=X"}, "currency": "JPY", "pair": "USD/JPY"},
    "KRW=X": {"returned": {"USDKRW=X"}, "currency": "KRW", "pair": "USD/KRW"},
    "CNY=X": {"returned": {"USDCNY=X"}, "currency": "CNY", "pair": "USD/CNY"},
}

CSV_FIELDS = [
    "run_id", "captured_at_kst", "group", "name", "symbol", "instrument_type", "value",
    "unit", "currency", "as_of_kst", "quote_age_minutes", "freshness", "fetch_status",
    "previous_close", "day_change_pct", "day_high", "day_low", "volume",
    "exchange_timezone", "yahoo_symbol", "yahoo_short_name", "error_code", "error",
]

ACCESS_LIMIT_CODES = {401, 403, 429}
SERVER_ERROR_CODES = {500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, code: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def iso_kst(dt: datetime) -> str:
    return dt.astimezone(KST).isoformat(timespec="seconds")


def num(value) -> str:
    """원래 값을 손대지 않고 문자열로 남긴다 (None -> 빈칸)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    return repr(value) if isinstance(value, float) else str(value)


def request_chart(symbol: str, timeout: float) -> dict:
    url = BASE_URL + quote(symbol, safe="") + "?" + urlencode({"interval": "1m", "range": "1d"})
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise FetchError("invalid_json", f"invalid JSON: {exc}") from exc


def fetch_once(symbol: str, timeout: float) -> dict:
    try:
        return request_chart(symbol, timeout)
    except HTTPError as exc:
        status = exc.code
        if status in ACCESS_LIMIT_CODES:
            raise FetchError("access_limit", f"HTTP {status}", status) from exc
        if status in SERVER_ERROR_CODES:
            raise FetchError("server_error", f"HTTP {status}", status) from exc
        raise FetchError("http_error", f"HTTP {status}", status) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise FetchError("timeout", f"timeout: {exc}") from exc
    except URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise FetchError("timeout", f"timeout: {exc.reason}") from exc
        raise FetchError("network_error", f"network error: {exc.reason}") from exc
    except FetchError:
        raise
    except (ConnectionError, OSError) as exc:
        raise FetchError("network_error", f"network error: {exc}") from exc


def check_identity(inst: Instrument, meta: dict) -> None:
    returned = str(meta.get("symbol") or "")
    if returned.upper() == inst.symbol.upper():
        return
    alias = FX_ALIASES.get(inst.symbol)
    if alias and returned.upper() in alias["returned"]:
        names = " ".join(str(meta.get(k) or "") for k in ("shortName", "longName")).upper()
        if str(meta.get("currency") or "").upper() == alias["currency"] and alias["pair"] in names:
            return
    raise FetchError("symbol_mismatch", f"requested {inst.symbol}, Yahoo returned {returned or '(none)'}")


def parse_meta(inst: Instrument, payload: dict) -> dict:
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise FetchError("malformed_response", "missing chart object")
    if chart.get("error"):
        raise FetchError("yahoo_error", json.dumps(chart["error"], ensure_ascii=False)[:300])
    result = chart.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise FetchError("malformed_response", "missing chart.result[0]")
    meta = result[0].get("meta")
    if not isinstance(meta, dict):
        raise FetchError("malformed_response", "missing chart.result[0].meta")
    check_identity(inst, meta)
    price = meta.get("regularMarketPrice")
    ts = meta.get("regularMarketTime")
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        raise FetchError("missing_price", "regularMarketPrice missing")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        # 7절: regularMarketTime이 없으면 as_of를 만들지 않는다 -> 실패 처리
        raise FetchError("missing_as_of", "regularMarketTime missing")
    return meta


def build_row(run_id: str, captured: datetime, inst: Instrument) -> dict:
    row = {k: "" for k in CSV_FIELDS}
    row.update(
        run_id=run_id,
        captured_at_kst=iso_kst(captured),
        group=inst.group,
        name=inst.name,
        symbol=inst.symbol,
        instrument_type=inst.kind,
        unit=inst.unit,
    )
    return row


def fill_success(row: dict, meta: dict, captured: datetime) -> None:
    price = meta["regularMarketPrice"]
    as_of = datetime.fromtimestamp(int(meta["regularMarketTime"]), tz=timezone.utc)
    age = (captured - as_of).total_seconds() / 60.0
    prev = meta.get("chartPreviousClose")
    if not isinstance(prev, (int, float)) or isinstance(prev, bool) or prev == 0:
        prev = meta.get("previousClose")
    change = ""
    if isinstance(prev, (int, float)) and not isinstance(prev, bool) and prev != 0:
        change = repr(round((price / prev - 1) * 100, 6))
    row.update(
        value=num(price),
        currency=str(meta.get("currency") or ""),
        as_of_kst=iso_kst(as_of),
        quote_age_minutes=f"{age:.1f}",
        freshness="fresh" if age <= FRESH_MAX_MINUTES else "old_quote_or_closed",
        fetch_status="ok",
        previous_close=num(prev) if isinstance(prev, (int, float)) else "",
        day_change_pct=change,
        day_high=num(meta.get("regularMarketDayHigh")),
        day_low=num(meta.get("regularMarketDayLow")),
        volume=num(meta.get("regularMarketVolume")),
        exchange_timezone=str(meta.get("exchangeTimezoneName") or ""),
        yahoo_symbol=str(meta.get("symbol") or ""),
        yahoo_short_name=str(meta.get("shortName") or meta.get("longName") or ""),
    )


def write_csv(path: str, rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def collect(output_dir: str, timeout: float, attempts: int, delay: float,
            access_wait: float, sleep=time.sleep, now=None) -> str:
    now = now or (lambda: datetime.now(timezone.utc))
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "yahoo_market_latest.csv")
    started = now().replace(microsecond=0)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    gh_run = os.environ.get("GITHUB_RUN_ID")
    if gh_run:
        run_id += f"-gh{gh_run}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"

    rows: list[dict] = []
    blocked = False
    for idx, inst in enumerate(INSTRUMENTS):
        row = build_row(run_id, started, inst)
        if blocked:
            row.update(fetch_status="not_attempted", error_code="stopped_after_access_limit",
                       error="earlier persistent access refusal")
            rows.append(row)
            continue
        if idx:
            sleep(delay)

        access_retry_used = False
        tries = 0
        last_err: FetchError | None = None
        while True:
            tries += 1
            try:
                payload = fetch_once(inst.symbol, timeout)
                meta = parse_meta(inst, payload)
                fill_success(row, meta, now())
                last_err = None
                break
            except FetchError as err:
                last_err = err
                if err.code == "access_limit":
                    if access_retry_used:
                        blocked = True
                        break
                    # 5절: 지금까지 결과 먼저 저장 -> cooldown -> 1회만 재시도
                    write_csv(path, rows)
                    access_retry_used = True
                    sleep(access_wait)
                    continue
                if err.code in ("server_error", "timeout", "network_error") and tries < attempts:
                    sleep(2 * (2 ** (tries - 1)))  # 2s -> 4s
                    continue
                break
        if last_err is not None:
            row.update(fetch_status="failed", error_code=last_err.code, error=str(last_err)[:300])
        rows.append(row)
        write_csv(path, rows)

    write_csv(path, rows)
    ok = sum(r["fetch_status"] == "ok" for r in rows)
    failed = sum(r["fetch_status"] == "failed" for r in rows)
    skipped = sum(r["fetch_status"] == "not_attempted" for r in rows)
    print(f"run_id={run_id} expected={len(rows)} success={ok} failed={failed} not_attempted={skipped}")
    for r in rows:
        if r["fetch_status"] != "ok":
            print(f"  {r['symbol']}: {r['fetch_status']} {r['error_code']} {r['error']}")
    return path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--timeout", type=float, default=8)
    p.add_argument("--attempts", type=int, default=2)
    p.add_argument("--delay", type=float, default=0.8)
    p.add_argument("--access-wait-seconds", type=float, default=15)
    args = p.parse_args(argv)
    collect(args.output_dir, args.timeout, max(1, args.attempts), args.delay, args.access_wait_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
