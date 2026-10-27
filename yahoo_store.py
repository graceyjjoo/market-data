#!/usr/bin/env python3
"""Canonical store for Yahoo captures.

Market Data Extraction Guide 13~27절 구현 (store + ops 역할을 한 파일에 담음).

  --csv FILE --store DIR   transient CSV -> canonical store (latest / history / runs)
  --validate --store DIR   canonical store 검증
  --derive --store DIR     health.json / monitoring_input.json / reporting_input.json 생성
  --audit --store DIR      전체 감사 (store + derived 일치 여부)
  --selftest               가짜 데이터로 자체 점검 (Yahoo 호출 없음)

표준 라이브러리만 사용한다.
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from yahoo_market_collector import INSTRUMENTS

SCHEMA_ID = "market-yahoo-v1"
SCHEMA_VERSION = 1
SOURCE = "yahoo_chart_v8"
PRICE_FIELD = "regularMarketPrice"
KST = timezone(timedelta(hours=9))

# 23절: forward-only 교체. 구 티커 -> 신 티커 (시계열 연결 금지)
SUPERSEDED = {"QTUM": "QTUP", "XAR": "WARP", "GRID": "JOUL", "URA": "NLR", "KOID": "HUMN"}

CATALOG = {i.symbol: i for i in INSTRUMENTS}
MONITOR_LOOKBACK_MIN = 90      # 매시 수집 + cron 지연을 감안한 "약 60분" 창
REPORT_CUTOFFS_KST = ((6, 30, "morning", "07:00"), (18, 30, "evening", "19:00"))


# ------------------------------------------------------------------ helpers
def parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {s}")
    return dt


def z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def series_id(symbol: str, kind: str, unit: str) -> str:
    return f"YAHOO:chart:{symbol}:{kind}:{unit}:{PRICE_FIELD}"


def observation_id(sid: str, as_of: str, value_raw: str) -> str:
    key = "|".join([sid, as_of, value_raw, PRICE_FIELD])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def fnum(s: str):
    if s is None or s == "":
        return None
    v = float(s)
    return v if math.isfinite(v) else None


def read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def history_files(store: str) -> list[str]:
    return sorted(glob.glob(os.path.join(store, "history", "*.jsonl")))


def iter_history(store: str, min_partition: str | None = None):
    for path in history_files(store):
        part = os.path.basename(path)[:-6]
        if min_partition and part < min_partition:
            continue
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if line:
                    yield path, lineno, line


def load_history_ids(store: str) -> set[str]:
    ids = set()
    for _, _, line in iter_history(store):
        try:
            ids.add(json.loads(line)["observation_id"])
        except (ValueError, KeyError):
            pass
    return ids


# ------------------------------------------------------------------ ingest
def ingest(csv_path: str, store: str) -> str:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit(f"empty capture CSV: {csv_path}")
    run_ids = {r["run_id"] for r in rows}
    if len(run_ids) != 1:
        raise SystemExit(f"CSV must contain exactly one run_id, found {sorted(run_ids)}")
    run_id = run_ids.pop()
    captured_kst = parse_ts(rows[0]["captured_at_kst"])
    captured_at = z(captured_kst)
    part = captured_kst.astimezone(KST).strftime("%Y-%m-%d")

    run_path = os.path.join(store, "runs", part, f"{run_id}.json")
    if os.path.exists(run_path):
        print(f"[ingest] run {run_id} already stored -> skip (idempotent)")
        return "skipped"

    existing = load_history_ids(store)
    new_obs, attempts = [], []
    for r in rows:
        sym = r["symbol"]
        sid = series_id(sym, r["instrument_type"], r["unit"])
        att = {
            "symbol": sym, "fetch_status": r["fetch_status"], "error_code": r["error_code"] or None,
            "error": r["error"] or None, "observation_id": None, "added": False,
        }
        if r["fetch_status"] == "ok":
            value = fnum(r["value"])
            if value is None or not r["as_of_kst"]:
                att.update(fetch_status="failed", error_code="invalid_row", error="missing value or as_of")
                attempts.append(att)
                continue
            as_of = z(parse_ts(r["as_of_kst"]))
            oid = observation_id(sid, as_of, r["value"])
            obs = {
                "observation_id": oid, "series_id": sid, "source": SOURCE, "symbol": sym,
                "name": r["name"], "group": r["group"], "instrument_type": r["instrument_type"],
                "unit": r["unit"], "currency": r["currency"] or None, "price_field": PRICE_FIELD,
                "value": value, "value_raw": r["value"], "as_of": as_of, "retrieved_at": captured_at,
                "run_id": run_id, "freshness_at_capture": r["freshness"],
                "previous_close": fnum(r["previous_close"]), "day_change_pct": fnum(r["day_change_pct"]),
                "day_high": fnum(r["day_high"]), "day_low": fnum(r["day_low"]),
                "volume": fnum(r["volume"]), "exchange_timezone": r["exchange_timezone"] or None,
                "yahoo_symbol": r["yahoo_symbol"] or None, "yahoo_short_name": r["yahoo_short_name"] or None,
            }
            att.update(observation_id=oid, as_of=as_of, value=value, freshness=r["freshness"])
            if oid not in existing:
                existing.add(oid)
                new_obs.append(obs)
                att["added"] = True
            att["_obs"] = obs
        attempts.append(att)

    # 1) history append-only
    if new_obs:
        hist = os.path.join(store, "history", f"{part}.jsonl")
        os.makedirs(os.path.dirname(hist), exist_ok=True)
        with open(hist, "a", encoding="utf-8") as fh:
            for o in new_obs:
                fh.write(json.dumps(o, ensure_ascii=False, sort_keys=True) + "\n")

    # 2) immutable run report
    counts = {
        "expected": len(rows),
        "success": sum(a["fetch_status"] == "ok" for a in attempts),
        "failed": sum(a["fetch_status"] == "failed" for a in attempts),
        "not_attempted": sum(a["fetch_status"] == "not_attempted" for a in attempts),
    }
    status = "success" if counts["success"] == counts["expected"] else ("partial" if counts["success"] else "failed")
    report = {
        "schema_id": SCHEMA_ID, "schema_version": SCHEMA_VERSION, "run_id": run_id,
        "captured_at": captured_at, "status": status, "counts": counts,
        "attempts": [{k: v for k, v in a.items() if k != "_obs"} for a in attempts],
        "observation_ids_added": [o["observation_id"] for o in new_obs],
    }
    write_json(run_path, report)

    # 3) latest (never rolls back)
    update_latest(store, run_id, captured_at, status, counts, attempts)
    print(f"[ingest] run {run_id} status={status} counts={counts} new_observations={len(new_obs)}")
    return status


def update_latest(store, run_id, captured_at, status, counts, attempts) -> None:
    path = os.path.join(store, "latest.json")
    latest = read_json(path) if os.path.exists(path) else {
        "schema_version": SCHEMA_VERSION, "schema_id": SCHEMA_ID,
        "latest_completed_run": None, "instruments": {},
    }
    lcr = latest.get("latest_completed_run")
    if lcr is None or captured_at > lcr["captured_at"]:
        latest["latest_completed_run"] = {"run_id": run_id, "captured_at": captured_at,
                                          "status": status, "counts": counts}

    instruments = latest.setdefault("instruments", {})
    for a in attempts:
        sym = a["symbol"]
        inst = CATALOG.get(sym)
        entry = instruments.setdefault(sym, {
            "name": inst.name if inst else sym, "group": inst.group if inst else None,
            "instrument_type": inst.kind if inst else None, "unit": inst.unit if inst else None,
            "series_id": series_id(sym, inst.kind, inst.unit) if inst else None,
            "last_valid": None, "last_attempt": None,
        })
        attempt = {"run_id": run_id, "captured_at": captured_at, "fetch_status": a["fetch_status"],
                   "error_code": a["error_code"], "observation_id": a["observation_id"]}
        la = entry.get("last_attempt")
        if la is None or captured_at >= la["captured_at"]:
            entry["last_attempt"] = attempt

        obs = a.get("_obs")
        if obs is None:
            continue
        lv = entry.get("last_valid")
        if lv and lv["observation_id"] == obs["observation_id"]:
            if captured_at > lv.get("last_confirmed_at", lv["retrieved_at"]):
                lv["last_confirmed_at"] = captured_at
            continue
        if lv is None or obs["as_of"] > lv["as_of"]:
            entry["last_valid"] = {
                "observation_id": obs["observation_id"], "value": obs["value"], "as_of": obs["as_of"],
                "retrieved_at": obs["retrieved_at"], "last_confirmed_at": obs["retrieved_at"],
                "run_id": run_id, "currency": obs["currency"], "previous_close": obs["previous_close"],
                "day_change_pct": obs["day_change_pct"], "freshness_at_capture": obs["freshness_at_capture"],
            }

    # 23절: 대체 티커가 실제로 수집된 뒤에만 구 티커를 latest에서 제거 (history/runs는 유지)
    for old, new in SUPERSEDED.items():
        if old in instruments and (instruments.get(new) or {}).get("last_valid"):
            del instruments[old]

    write_json(path, latest)


# ------------------------------------------------------------------ validate
REQUIRED_OBS = ("observation_id", "series_id", "symbol", "value", "value_raw", "as_of", "retrieved_at", "run_id")


def validate(store: str) -> dict:
    errors: list[str] = []
    path = os.path.join(store, "latest.json")
    if not os.path.exists(path):
        return {"status": "invalid", "errors": ["latest.json missing"], "history_lines": 0, "runs": 0}
    latest = read_json(path)
    if latest.get("schema_id") != SCHEMA_ID or latest.get("schema_version") != SCHEMA_VERSION:
        errors.append("latest.json schema mismatch")

    ids: dict[str, dict] = {}
    lines = 0
    for fpath, lineno, line in iter_history(store):
        lines += 1
        where = f"{os.path.basename(fpath)}:{lineno}"
        try:
            o = json.loads(line)
        except ValueError:
            errors.append(f"{where} invalid JSON")
            continue
        missing = [k for k in REQUIRED_OBS if k not in o]
        if missing:
            errors.append(f"{where} missing {missing}")
            continue
        if observation_id(o["series_id"], o["as_of"], o["value_raw"]) != o["observation_id"]:
            errors.append(f"{where} observation_id mismatch")
        if o["observation_id"] in ids:
            errors.append(f"{where} duplicate observation_id")
        ids[o["observation_id"]] = o

    run_files = glob.glob(os.path.join(store, "runs", "*", "*.json"))
    run_index = {}
    for rp in run_files:
        try:
            rep = read_json(rp)
        except ValueError:
            errors.append(f"{rp} invalid JSON")
            continue
        run_index[rep.get("run_id")] = rep
        for oid in rep.get("observation_ids_added", []):
            if oid not in ids:
                errors.append(f"run {rep.get('run_id')} references missing observation {oid[:12]}")

    lcr = latest.get("latest_completed_run")
    if lcr:
        rep = run_index.get(lcr["run_id"])
        if rep is None:
            errors.append(f"latest_completed_run {lcr['run_id']} has no run file")
        elif rep.get("counts") != lcr.get("counts") or rep.get("captured_at") != lcr.get("captured_at"):
            errors.append("latest_completed_run does not match its run file")

    for sym, entry in (latest.get("instruments") or {}).items():
        lv = entry.get("last_valid")
        if lv:
            o = ids.get(lv["observation_id"])
            if o is None:
                errors.append(f"{sym} last_valid not in history")
            elif o["value"] != lv["value"] or o["as_of"] != lv["as_of"]:
                errors.append(f"{sym} last_valid differs from history")
        la = entry.get("last_attempt")
        if la and la["run_id"] not in run_index:
            errors.append(f"{sym} last_attempt run {la['run_id']} has no run file")

    return {"status": "valid" if not errors else "invalid", "errors": errors[:50],
            "error_count": len(errors), "history_lines": lines, "runs": len(run_index)}


# ------------------------------------------------------------------ derive
def load_recent(store: str, since: datetime) -> dict[str, list[dict]]:
    """as_of >= since 인 관측치를 symbol별로 (as_of 오름차순)."""
    min_part = (since.astimezone(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
    out: dict[str, list[dict]] = {}
    since_z = z(since)
    for _, _, line in iter_history(store, min_part):
        o = json.loads(line)
        if o["as_of"] >= since_z:
            out.setdefault(o["symbol"], []).append(o)
    for v in out.values():
        v.sort(key=lambda o: (o["as_of"], o["retrieved_at"]))
    return out


def run_reports(store: str) -> list[dict]:
    reps = []
    for rp in glob.glob(os.path.join(store, "runs", "*", "*.json")):
        rep = read_json(rp)
        reps.append({"run_id": rep["run_id"], "captured_at": rep["captured_at"], "status": rep["status"]})
    reps.sort(key=lambda r: r["captured_at"])
    return reps


def pct(a: float, b: float):
    return None if not b else round((a / b - 1) * 100, 4)


def path_stats(obs: list[dict], sid: str, start: datetime, end: datetime, baseline_ok: bool) -> dict:
    """start~end 창의 가격 경로. 같은 series_id만 사용, as_of 기준."""
    start_z, end_z = z(start), z(end)
    same = [o for o in obs if o["series_id"] == sid and o["as_of"] <= end_z]
    inside = [o for o in same if o["as_of"] > start_z]
    base = None
    if baseline_ok:
        before = [o for o in same if o["as_of"] <= start_z]
        base = before[-1] if before else None
    points = ([base] if base else []) + inside
    distinct = sorted({o["as_of"] for o in points})
    res = {"path_available": False, "reason": None, "n_quotes_in_window": len({o["as_of"] for o in inside})}
    if len(distinct) < 2 or not inside:
        res["reason"] = "no_new_recent_path_quotes" if not inside else "single_quote_only"
        if points:
            res["last_as_of"] = points[-1]["as_of"]
            res["last_value"] = points[-1]["value"]
        return res
    first, last = points[0], points[-1]
    vals = [o["value"] for o in points]
    res.update(
        path_available=True, reason=None,
        start_value=first["value"], start_as_of=first["as_of"],
        start_is_pre_window_baseline=bool(base) and first is base,
        end_value=last["value"], end_as_of=last["as_of"],
        span_minutes=round((parse_ts(last["as_of"]) - parse_ts(first["as_of"])).total_seconds() / 60, 1),
        change_pct=pct(last["value"], first["value"]),
        sampled_high=max(vals), sampled_low=min(vals),
    )
    return res


def report_windows(ref: datetime) -> list[dict]:
    ref_kst = ref.astimezone(KST)
    cands = []
    for d in (-1, 0, 1):
        day = (ref_kst + timedelta(days=d)).date()
        for h, m, label, rtime in REPORT_CUTOFFS_KST:
            cands.append((datetime(day.year, day.month, day.day, h, m, tzinfo=KST), label, rtime))
    cands.sort()
    past = [c for c in cands if c[0] <= ref_kst][-1]
    nxt = [c for c in cands if c[0] > ref_kst][0]
    return [past, nxt]


def derive(store: str) -> None:
    latest = read_json(os.path.join(store, "latest.json"))
    lcr = latest["latest_completed_run"]
    ref = parse_ts(lcr["captured_at"])
    audit = validate(store)
    instruments = latest.get("instruments", {})

    # health.json
    health_inst = {}
    for sym, e in instruments.items():
        lv, la = e.get("last_valid") or {}, e.get("last_attempt") or {}
        age = None
        if lv:
            age = round((ref - parse_ts(lv["as_of"])).total_seconds() / 60, 1)
        health_inst[sym] = {
            "name": e.get("name"), "group": e.get("group"),
            "last_valid_value": lv.get("value"), "last_valid_as_of": lv.get("as_of"),
            "quote_age_minutes_at_run": age,
            "stale_or_closed": (age is not None and age > 30),
            "last_attempt_status": la.get("fetch_status"), "last_attempt_error_code": la.get("error_code"),
            "last_attempt_captured_at": la.get("captured_at"),
        }
    reps = run_reports(store)
    health = {
        "schema_id": "market-yahoo-health-v1", "source_run_id": lcr["run_id"],
        "source_captured_at": lcr["captured_at"], "run_status": lcr["status"], "counts": lcr["counts"],
        "active_universe_size": len(CATALOG), "runs_stored": len(reps),
        "audit": {"status": audit["status"], "error_count": audit["error_count"], "errors": audit["errors"][:20],
                  "history_lines": audit["history_lines"]},
        "note": "fetch success != live tick. stale_or_closed quotes are not current prices.",
        "instruments": health_inst,
    }
    write_json(os.path.join(store, "health.json"), health)

    # monitoring_input.json (약 60분, source as_of 기준)
    mon_start = ref - timedelta(minutes=MONITOR_LOOKBACK_MIN)
    recent = load_recent(store, mon_start - timedelta(days=4))
    mon = {}
    for sym, inst in CATALOG.items():
        sid = series_id(sym, inst.kind, inst.unit)
        st = path_stats(recent.get(sym, []), sid, mon_start, ref, baseline_ok=False)
        if inst.kind == "futures":
            st["contract_verified"] = False
            st["contract_note"] = "Yahoo continuous futures symbol; contract roll not verified"
        lv = (instruments.get(sym) or {}).get("last_valid") or {}
        mon[sym] = {"name": inst.name, "group": inst.group, "instrument_type": inst.kind, "unit": inst.unit,
                    "series_id": sid, "snapshot": {k: lv.get(k) for k in ("value", "as_of", "day_change_pct",
                                                                         "previous_close", "currency")},
                    "path": st}
    write_json(os.path.join(store, "monitoring_input.json"), {
        "schema_id": "market-yahoo-monitoring-v1", "source_run_id": lcr["run_id"],
        "reference_time": lcr["captured_at"], "window_start": z(mon_start), "window_end": z(ref),
        "window_basis": "source as_of (not capture time)",
        "rule": "path_available=false means no measurable path; do NOT read it as 0% change",
        "instruments": mon,
    })

    # reporting_input.json (07:00 / 19:00 보고용 12시간 창)
    windows = []
    for cutoff, label, rtime in report_windows(ref):
        start = cutoff - timedelta(hours=12)
        reached = cutoff <= ref
        before = [r for r in reps if parse_ts(r["captured_at"]) <= cutoff]
        last_before = before[-1] if before else None
        gap = round((cutoff - parse_ts(last_before["captured_at"])).total_seconds() / 60, 1) if last_before else None
        data_end = min(cutoff, ref)
        insts = {}
        for sym, inst in CATALOG.items():
            sid = series_id(sym, inst.kind, inst.unit)
            insts[sym] = {"name": inst.name, "group": inst.group,
                          "path": path_stats(recent.get(sym, []), sid, start, data_end, baseline_ok=True)}
        windows.append({
            "report": label, "report_time_kst": rtime,
            "window_start": z(start), "window_end": z(cutoff),
            "cutoff_reached_at_collection": reached,
            "last_collection_before_cutoff": last_before["run_id"] if last_before else None,
            "last_collection_before_cutoff_gap_minutes": gap,
            "note": None if reached else "cutoff not yet reached; data ends at latest collection, no interpolation",
            "instruments": insts,
        })
    # 확인용 recent 범위가 4일 전부터이므로 baseline은 최대 4일 전까지만 찾는다
    write_json(os.path.join(store, "reporting_input.json"), {
        "schema_id": "market-yahoo-reporting-v1", "source_run_id": lcr["run_id"],
        "reference_time": lcr["captured_at"], "window_basis": "source as_of; change = last quote <= window_end "
        "vs last quote <= window_start (baseline, up to 4 days back)", "windows": windows,
    })
    print(f"[derive] source_run={lcr['run_id']} audit={audit['status']}")


# ------------------------------------------------------------------ audit
def audit(store: str) -> dict:
    res = validate(store)
    errors = list(res["errors"])
    latest = read_json(os.path.join(store, "latest.json"))
    rid = (latest.get("latest_completed_run") or {}).get("run_id")
    for name in ("health.json", "monitoring_input.json", "reporting_input.json"):
        p = os.path.join(store, name)
        if not os.path.exists(p):
            errors.append(f"{name} missing")
        elif read_json(p).get("source_run_id") != rid:
            errors.append(f"{name} source_run_id != latest_completed_run")
    return {"status": "valid" if not errors else "invalid", "errors": errors}


# ------------------------------------------------------------------ selftest
def _fake_csv(path, run_id, captured, quotes, statuses=None):
    import yahoo_market_collector as c
    rows = []
    for inst in INSTRUMENTS:
        row = c.build_row(run_id, captured, inst)
        st = (statuses or {}).get(inst.symbol, "ok")
        if st == "ok":
            value, as_of = quotes(inst.symbol)
            row.update(value=repr(value), currency="USD", as_of_kst=c.iso_kst(as_of),
                       quote_age_minutes="1.0", freshness="fresh", fetch_status="ok",
                       previous_close="100.0", day_change_pct=repr(round(value - 100.0, 6)),
                       yahoo_symbol=inst.symbol)
        else:
            row.update(fetch_status=st, error_code="test")
        rows.append(row)
    c.write_csv(path, rows)


def selftest() -> int:
    tmp = tempfile.mkdtemp(prefix="yahoo_selftest_")
    store = os.path.join(tmp, "store")
    try:
        t0 = datetime(2026, 10, 27, 0, 10, tzinfo=timezone.utc)
        t1 = t0 + timedelta(hours=1)
        csv0, csv1, csv_old = (os.path.join(tmp, n) for n in ("a.csv", "b.csv", "old.csv"))
        _fake_csv(csv0, "R0", t0, lambda s: (100.0, t0 - timedelta(minutes=2)))
        # R1: SOXX 갱신, ^KS11은 같은 quote 반복(stale), XLF 실패
        _fake_csv(csv1, "R1", t1, lambda s: (101.5 if s != "^KS11" else 100.0,
                                             t1 - timedelta(minutes=2) if s != "^KS11" else t0 - timedelta(minutes=2)),
                  statuses={"XLF": "failed"})
        _fake_csv(csv_old, "ROLD", t0 - timedelta(hours=5), lambda s: (99.0, t0 - timedelta(hours=5, minutes=2)))
        assert ingest(csv0, store) == "success"
        assert ingest(csv1, store) == "partial"
        assert ingest(csv1, store) == "skipped"
        assert ingest(csv_old, store) == "success"
        latest = read_json(os.path.join(store, "latest.json"))
        assert latest["latest_completed_run"]["run_id"] == "R1", "latest rolled back"
        assert latest["instruments"]["XLF"]["last_valid"]["value"] == 100.0, "last_valid lost on failure"
        assert latest["instruments"]["XLF"]["last_attempt"]["fetch_status"] == "failed"
        assert latest["instruments"]["SOXX"]["last_valid"]["value"] == 101.5
        assert latest["instruments"]["^KS11"]["last_valid"]["as_of"] == z(t0 - timedelta(minutes=2)), "stale re-stamped"
        n_lines = sum(1 for _ in iter_history(store))
        assert n_lines == 48 + 46 + 48, f"unexpected history lines {n_lines}"
        v = validate(store)
        assert v["status"] == "valid", v
        derive(store)
        a = audit(store)
        assert a["status"] == "valid", a
        mon = read_json(os.path.join(store, "monitoring_input.json"))
        assert mon["instruments"]["SOXX"]["path"]["path_available"] is True
        assert abs(mon["instruments"]["SOXX"]["path"]["change_pct"] - 1.5) < 1e-9
        assert mon["instruments"]["^KS11"]["path"]["path_available"] is False
        print("[selftest] OK")
        return 0
    except AssertionError as exc:
        print(f"[selftest] FAILED: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", default="market/yahoo")
    p.add_argument("--csv", nargs="+")
    p.add_argument("--validate", action="store_true")
    p.add_argument("--derive", action="store_true")
    p.add_argument("--audit", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.csv:
        for path in a.csv:
            ingest(path, a.store)
    if a.validate:
        res = validate(a.store)
        print(f"[validate] {res['status']} history_lines={res['history_lines']} runs={res['runs']}")
        for e in res["errors"]:
            print("  -", e)
        if res["status"] != "valid":
            return 1
    if a.derive:
        derive(a.store)
    if a.audit:
        res = audit(a.store)
        print(f"[audit] {res['status']}")
        for e in res["errors"]:
            print("  -", e)
        if res["status"] != "valid":
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
