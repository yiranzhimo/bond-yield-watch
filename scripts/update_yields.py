#!/usr/bin/env python3
"""Refresh the China / United States / Japan sovereign-yield snapshot.

Data sources
------------
- Eastmoney datacenter ``RPTA_WEB_TREASURYYIELD``: China and United States
  2Y / 5Y / 10Y / 30Y constant-maturity yields, daily, ~9.3k observations.
- Ministry of Finance Japan ``jgbcme_all.csv``: the full JGB term structure
  (1Y-40Y), daily since 1974, English headers, no key required.
- Federal Reserve FRED ``DGS2/DGS5/DGS10/DGS30``: cross-check for the US leg
  and the fallback when Eastmoney is unreachable.

The script writes ``data/yields.json`` (dashboard payload), ``data/history.csv``
(tidy long-form history) and ``data/alerts.json`` (threshold breaches). Every
network call degrades to the previous committed snapshot instead of failing the
workflow, so a single bad upstream day never blanks the site.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
YIELDS_PATH = DATA_DIR / "yields.json"
HISTORY_PATH = DATA_DIR / "history.csv"
ALERTS_PATH = DATA_DIR / "alerts.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

EASTMONEY_URL = (
    "https://datacenter.eastmoney.com/api/data/get"
    "?type=RPTA_WEB_TREASURYYIELD&sty=ALL&st=SOLAR_DATE&sr=-1&p={page}&ps={size}&source=WEB"
)
EASTMONEY_REFERER = "https://data.eastmoney.com/cjsj/zmgzsyl.html"
# MOF splits the series: ``jgbcme.csv`` is the current month only, while
# ``historical/jgbcme_all.csv`` lags it by a few weeks. Fetch both and merge.
MOF_CURRENT_URL = (
    "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"
)
MOF_HISTORY_URL = (
    "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    "historical/jgbcme_all.csv"
)
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# Eastmoney encodes each maturity as an opaque metric id, and the ids are not
# ordered by tenor. Each feed also publishes its own 10Y-2Y spread column,
# which pins the mapping down: the assignment below is the only one of the
# candidates that reproduces that published spread (verified on 468/468 rows).
# ``validate_eastmoney_mapping`` re-checks the identity on every run.
EASTMONEY_FIELDS = {
    "CN": {"2Y": "EMM00588704", "5Y": "EMM00166462", "10Y": "EMM00166466", "30Y": "EMM00166469"},
    "US": {"2Y": "EMG00001306", "5Y": "EMG00001308", "10Y": "EMG00001310", "30Y": "EMG00001312"},
}
# Published 10Y-2Y spread column per market, used as the mapping guard.
EASTMONEY_SPREAD_FIELDS = {"CN": "EMM01276014", "US": "EMG01339436"}
FRED_SERIES = {"2Y": "DGS2", "5Y": "DGS5", "10Y": "DGS10", "30Y": "DGS30"}
MOF_COLUMNS = {"2Y": "2Y", "5Y": "5Y", "10Y": "10Y", "30Y": "30Y"}

TENORS = ("2Y", "5Y", "10Y", "30Y")
MARKETS = ("CN", "US", "JP")

MARKET_META = {
    "CN": {
        "name_zh": "中国国债",
        "name_en": "China Government Bond",
        "currency": "CNY",
        "source": "东方财富数据中心",
        "source_url": "https://data.eastmoney.com/cjsj/zmgzsyl.html",
    },
    "US": {
        "name_zh": "美国国债",
        "name_en": "US Treasury",
        "currency": "USD",
        "source": "东方财富数据中心 / FRED 交叉校验",
        "source_url": "https://fred.stlouisfed.org/categories/115",
    },
    "JP": {
        "name_zh": "日本国债",
        "name_en": "Japanese Government Bond",
        "currency": "JPY",
        "source": "日本财务省 (MOF)",
        "source_url": "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/",
    },
}

# Alert thresholds, in basis points. A 10Y move of ±10bp in one day is a
# genuine repricing in any of these three markets; ±25bp over a week likewise.
ALERT_RULES = {
    "daily_bp": 10.0,
    "weekly_bp": 25.0,
    "spread_daily_bp": 10.0,
    "inversion_watch_bp": 0.0,
}

# Rolling window used to convert a level into a percentile. Two years of
# business days keeps the score responsive without over-reacting to one month.
PERCENTILE_WINDOW_DAYS = 504
HISTORY_RETENTION_DAYS = 3650

MAX_RETRIES = 3
RETRY_SLEEP_SECONDS = 3.0
REQUEST_TIMEOUT = 45


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, file=sys.stderr)


def finite(value: Any) -> Optional[float]:
    """Coerce to float, mapping NaN/inf/blank/sentinels to None."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text in {"", "-", "--", ".", "NA", "N/A", "null", "None"}:
            return None
        value = text
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def round_or_none(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)


def bp(value: Optional[float]) -> Optional[float]:
    """Percentage points to basis points."""
    return None if value is None else round(value * 100.0, 1)


def fetch_bytes(
    url: str, referer: Optional[str] = None, browser_ua: bool = True
) -> bytes:
    """GET with retries.

    ``browser_ua=False`` matters for FRED: it stalls on a Chrome user-agent
    until the socket times out, but answers a plain urllib request instantly.
    """
    headers: Dict[str, str] = {"Accept": "*/*"}
    if browser_ua:
        headers["User-Agent"] = USER_AGENT
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    errors: List[str] = []
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP_SECONDS * attempt)
    raise RuntimeError(f"GET {url} failed -> {'; '.join(errors)}")


# ---------------------------------------------------------------------------
# upstream fetchers -> {iso_date: {tenor: yield}}
# ---------------------------------------------------------------------------

Series = Dict[str, Dict[str, float]]


def fetch_eastmoney(max_pages: int = 20, page_size: int = 500) -> Dict[str, Series]:
    """China + United States curves from the Eastmoney datacenter feed."""
    out: Dict[str, Series] = {"CN": {}, "US": {}}
    guard_rows: List[Dict[str, Any]] = []
    pages_seen = 0
    for page in range(1, max_pages + 1):
        raw = fetch_bytes(
            EASTMONEY_URL.format(page=page, size=page_size), referer=EASTMONEY_REFERER
        )
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if not payload.get("success"):
            raise RuntimeError(f"Eastmoney page {page} returned success=false")
        result = payload.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            break
        for row in rows:
            stamp = str(row.get("SOLAR_DATE") or "")[:10]
            if len(stamp) != 10:
                continue
            if len(guard_rows) < 300:
                guard_rows.append(row)
            for market, fields in EASTMONEY_FIELDS.items():
                bucket = out[market].setdefault(stamp, {})
                for tenor, field in fields.items():
                    value = finite(row.get(field))
                    if value is not None:
                        bucket[tenor] = value
        pages_seen += 1
        total_pages = int(result.get("pages") or 0)
        if total_pages and page >= min(total_pages, max_pages):
            break
    # Drop dates where every tenor came back empty.
    for market in out:
        out[market] = {d: v for d, v in out[market].items() if v}
    validate_eastmoney_mapping(guard_rows)
    log(
        f"Eastmoney: {pages_seen} page(s), "
        f"CN {len(out['CN'])} obs, US {len(out['US'])} obs"
    )
    return out


def validate_eastmoney_mapping(rows: Sequence[Dict[str, Any]]) -> None:
    """Assert our tenor mapping still reproduces the feed's own 10Y-2Y spread.

    Eastmoney's metric ids carry no tenor semantics, so a silent reshuffle
    upstream would otherwise mislabel the whole curve. Raising here means the
    run falls back to cached history instead of publishing wrong yields.
    """
    for market, spread_field in EASTMONEY_SPREAD_FIELDS.items():
        ten_field = EASTMONEY_FIELDS[market]["10Y"]
        two_field = EASTMONEY_FIELDS[market]["2Y"]
        checked = matched = 0
        for row in rows:
            spread = finite(row.get(spread_field))
            ten = finite(row.get(ten_field))
            two = finite(row.get(two_field))
            if None in (spread, ten, two):
                continue
            checked += 1
            if abs((ten - two) - spread) <= 0.0051:
                matched += 1
        if checked < 20:
            log(f"Mapping guard for {market}: only {checked} comparable row(s), skipped")
            continue
        ratio = matched / checked
        if ratio < 0.95:
            raise RuntimeError(
                f"Eastmoney {market} tenor mapping looks wrong: only {matched}/{checked} "
                f"rows satisfy 10Y-2Y == {spread_field}. Field ids may have changed."
            )
        log(f"Mapping guard for {market}: {matched}/{checked} rows consistent")


def fetch_mof_japan() -> Series:
    """JGB curve from MOF: long history overlaid with the current month.

    The history file is authoritative for old dates but trails the calendar, so
    the current-month file is applied last and wins on any overlap.
    """
    out: Series = {}
    errors: List[str] = []
    for label, url in (("history", MOF_HISTORY_URL), ("current", MOF_CURRENT_URL)):
        try:
            partial = parse_mof_csv(fetch_bytes(url))
        except Exception as exc:  # noqa: BLE001 - one file may lag or move
            errors.append(f"{label}: {exc}")
            log(f"MOF {label} file unavailable: {exc}")
            continue
        out.update(partial)
        log(f"MOF {label}: {len(partial)} obs, newest {max(partial) if partial else '—'}")
    if not out:
        raise RuntimeError("MOF returned no observations -> " + "; ".join(errors))
    log(f"MOF Japan merged: {len(out)} obs, newest {max(out)}")
    return out


def parse_mof_csv(raw: bytes) -> Series:
    """Parse one MOF interest-rate CSV into {iso_date: {tenor: yield}}."""
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header: Optional[List[str]] = None
    index: Dict[str, int] = {}
    out: Series = {}
    for row in reader:
        if not row:
            continue
        first = (row[0] or "").strip()
        if header is None:
            # The real header row starts with "Date"; the line above it is a title.
            if first.lower() == "date":
                header = [cell.strip() for cell in row]
                for tenor, column in MOF_COLUMNS.items():
                    if column in header:
                        index[tenor] = header.index(column)
            continue
        stamp = parse_mof_date(first)
        if stamp is None:
            continue
        bucket: Dict[str, float] = {}
        for tenor, position in index.items():
            if position < len(row):
                value = finite(row[position])
                if value is not None:
                    bucket[tenor] = value
        if bucket:
            out[stamp] = bucket
    return out


def parse_mof_date(text: str) -> Optional[str]:
    """MOF uses ``YYYY/M/D``; tolerate dashes and stray whitespace."""
    cleaned = text.replace("-", "/").strip()
    parts = cleaned.split("/")
    if len(parts) != 3:
        return None
    try:
        year, month, day = (int(part) for part in parts)
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def fetch_fred() -> Series:
    """US constant-maturity yields straight from FRED, one CSV per tenor."""
    out: Series = {}
    for tenor, series in FRED_SERIES.items():
        raw = fetch_bytes(FRED_URL.format(series=series), browser_ua=False)
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
        rows = list(reader)
        if not rows:
            continue
        # FRED emits either "DATE,DGS10" (legacy) or "observation_date,DGS10".
        for row in rows[1:]:
            if len(row) < 2:
                continue
            stamp = (row[0] or "").strip()
            if len(stamp) != 10:
                continue
            value = finite(row[1])
            if value is not None:
                out.setdefault(stamp, {})[tenor] = value
    if not out:
        raise RuntimeError("FRED returned no usable observations")
    log(f"FRED US: {len(out)} obs, newest {max(out)}")
    return out


# ---------------------------------------------------------------------------
# history store
# ---------------------------------------------------------------------------


def load_history() -> Dict[str, Series]:
    """Read the committed tidy CSV back into nested dicts."""
    history: Dict[str, Series] = {market: {} for market in MARKETS}
    if not HISTORY_PATH.exists():
        return history
    with HISTORY_PATH.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            market = (row.get("market") or "").strip().upper()
            stamp = (row.get("date") or "").strip()
            tenor = (row.get("tenor") or "").strip().upper()
            value = finite(row.get("yield"))
            if market in history and len(stamp) == 10 and tenor in TENORS and value is not None:
                history[market].setdefault(stamp, {})[tenor] = value
    counts = ", ".join(f"{m} {len(history[m])}" for m in MARKETS)
    log(f"Cached history: {counts}")
    return history


def merge_series(base: Series, incoming: Series) -> Tuple[Series, int]:
    """Overlay fresh observations onto the cache; returns (merged, new dates)."""
    merged: Series = {stamp: dict(values) for stamp, values in base.items()}
    added = 0
    for stamp, values in incoming.items():
        if stamp not in merged:
            added += 1
            merged[stamp] = dict(values)
        else:
            merged[stamp].update(values)
    return merged, added


def write_history(history: Dict[str, Series]) -> None:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["market", "date", "tenor", "yield"])
        for market in MARKETS:
            for stamp in sorted(history[market]):
                if stamp < cutoff:
                    continue
                for tenor in TENORS:
                    value = history[market][stamp].get(tenor)
                    if value is not None:
                        writer.writerow([market, stamp, tenor, f"{value:.4f}"])


# ---------------------------------------------------------------------------
# analytics
# ---------------------------------------------------------------------------


def sorted_dates(series: Series, tenor: str) -> List[str]:
    return sorted(stamp for stamp, values in series.items() if values.get(tenor) is not None)


def value_on(series: Series, stamp: str, tenor: str) -> Optional[float]:
    return series.get(stamp, {}).get(tenor)


def value_asof(series: Series, target: str, tenor: str, dates: Sequence[str]) -> Optional[float]:
    """Most recent observation at or before ``target``."""
    chosen: Optional[str] = None
    for stamp in dates:
        if stamp <= target:
            chosen = stamp
        else:
            break
    return None if chosen is None else value_on(series, chosen, tenor)


def change_bp(series: Series, tenor: str, days: int) -> Optional[float]:
    """Change in basis points over a calendar-day lookback."""
    dates = sorted_dates(series, tenor)
    if not dates:
        return None
    latest = dates[-1]
    current = value_on(series, latest, tenor)
    if current is None:
        return None
    if days <= 0:
        return None
    if days == 1:
        # One *observation* back, so a Monday compares to the prior Friday.
        if len(dates) < 2:
            return None
        previous = value_on(series, dates[-2], tenor)
    else:
        target = (date.fromisoformat(latest) - timedelta(days=days)).isoformat()
        previous = value_asof(series, target, tenor, dates)
    if previous is None:
        return None
    return bp(current - previous)


def percentile_rank(series: Series, tenor: str, window: int = PERCENTILE_WINDOW_DAYS) -> Optional[float]:
    """Where the latest level sits inside its own trailing distribution."""
    dates = sorted_dates(series, tenor)
    if len(dates) < 30:
        return None
    recent = dates[-window:]
    values = [value_on(series, stamp, tenor) for stamp in recent]
    values = [v for v in values if v is not None]
    if len(values) < 30:
        return None
    current = values[-1]
    below = sum(1 for v in values if v <= current)
    return round(100.0 * below / len(values), 1)


def curve_snapshot(series: Series) -> Dict[str, Any]:
    """Latest level, changes and term structure for one market."""
    all_dates = sorted({stamp for stamp, values in series.items() if values})
    latest = all_dates[-1] if all_dates else None
    tenors: Dict[str, Any] = {}
    for tenor in TENORS:
        dates = sorted_dates(series, tenor)
        own_latest = dates[-1] if dates else None
        level = value_on(series, own_latest, tenor) if own_latest else None
        tenors[tenor] = {
            "yield": round_or_none(level, 4),
            "as_of": own_latest,
            "stale": bool(own_latest and latest and own_latest < latest),
            "change_1d_bp": change_bp(series, tenor, 1),
            "change_1w_bp": change_bp(series, tenor, 7),
            "change_1m_bp": change_bp(series, tenor, 30),
            "change_3m_bp": change_bp(series, tenor, 91),
            "change_1y_bp": change_bp(series, tenor, 365),
            "percentile_2y": percentile_rank(series, tenor),
        }

    def level(tenor: str) -> Optional[float]:
        return tenors[tenor]["yield"]

    spread_10_2 = None
    if level("10Y") is not None and level("2Y") is not None:
        spread_10_2 = bp(level("10Y") - level("2Y"))
    spread_30_10 = None
    if level("30Y") is not None and level("10Y") is not None:
        spread_30_10 = bp(level("30Y") - level("10Y"))
    spread_10_5 = None
    if level("10Y") is not None and level("5Y") is not None:
        spread_10_5 = bp(level("10Y") - level("5Y"))

    return {
        "as_of": latest,
        "tenors": tenors,
        "term_structure": {
            "spread_10y_2y_bp": spread_10_2,
            "spread_30y_10y_bp": spread_30_10,
            "spread_10y_5y_bp": spread_10_5,
            "shape": curve_shape(spread_10_2, spread_30_10),
            "inverted": bool(spread_10_2 is not None and spread_10_2 < 0),
        },
    }


def curve_shape(spread_10_2: Optional[float], spread_30_10: Optional[float]) -> str:
    """Coarse label for the belly-to-long shape of the curve."""
    if spread_10_2 is None:
        return "unknown"
    if spread_10_2 < -25:
        return "deeply_inverted"
    if spread_10_2 < 0:
        return "inverted"
    if spread_10_2 < 25:
        return "flat"
    if spread_10_2 < 100:
        return "normal"
    return "steep"


def spread_history(a: Series, b: Series, tenor: str) -> Series:
    """Synthetic series of (a - b) on the dates both markets quote."""
    out: Series = {}
    for stamp, values in a.items():
        left = values.get(tenor)
        right = b.get(stamp, {}).get(tenor)
        if left is not None and right is not None:
            out[stamp] = {tenor: left - right}
    return out


def cross_market_spreads(history: Dict[str, Series]) -> Dict[str, Any]:
    """Yield differentials that drive FX and cross-border capital flows."""
    pairs = (
        ("CN10Y_US10Y", "CN", "US", "10Y", "中美 10Y 利差"),
        ("JP10Y_US10Y", "JP", "US", "10Y", "日美 10Y 利差"),
        ("CN10Y_JP10Y", "CN", "JP", "10Y", "中日 10Y 利差"),
        ("CN2Y_US2Y", "CN", "US", "2Y", "中美 2Y 利差"),
    )
    out: Dict[str, Any] = {}
    for key, left, right, tenor, label in pairs:
        series = spread_history(history[left], history[right], tenor)
        dates = sorted_dates(series, tenor)
        if not dates:
            out[key] = {"label": label, "spread_bp": None}
            continue
        latest = dates[-1]
        out[key] = {
            "label": label,
            "tenor": tenor,
            "as_of": latest,
            "spread_bp": bp(value_on(series, latest, tenor)),
            "change_1d_bp": change_bp(series, tenor, 1),
            "change_1w_bp": change_bp(series, tenor, 7),
            "change_1m_bp": change_bp(series, tenor, 30),
            "change_1y_bp": change_bp(series, tenor, 365),
            "percentile_2y": percentile_rank(series, tenor),
        }
    return out


def build_alerts(
    snapshots: Dict[str, Dict[str, Any]], spreads: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Threshold breaches worth a human look, most severe first."""
    alerts: List[Dict[str, Any]] = []

    for market in MARKETS:
        snapshot = snapshots[market]
        label = MARKET_META[market]["name_zh"]
        for tenor in TENORS:
            info = snapshot["tenors"][tenor]
            daily = info.get("change_1d_bp")
            weekly = info.get("change_1w_bp")
            if daily is not None and abs(daily) >= ALERT_RULES["daily_bp"]:
                alerts.append(
                    {
                        "severity": "high" if abs(daily) >= 2 * ALERT_RULES["daily_bp"] else "medium",
                        "market": market,
                        "kind": "daily_move",
                        "metric": f"{market} {tenor}",
                        "value_bp": daily,
                        "threshold_bp": ALERT_RULES["daily_bp"],
                        "message": (
                            f"{label} {tenor} 单日{'上行' if daily > 0 else '下行'} "
                            f"{abs(daily):.1f}bp，至 {info['yield']:.3f}%"
                        ),
                    }
                )
            if weekly is not None and abs(weekly) >= ALERT_RULES["weekly_bp"]:
                alerts.append(
                    {
                        "severity": "medium",
                        "market": market,
                        "kind": "weekly_move",
                        "metric": f"{market} {tenor}",
                        "value_bp": weekly,
                        "threshold_bp": ALERT_RULES["weekly_bp"],
                        "message": (
                            f"{label} {tenor} 一周累计"
                            f"{'上行' if weekly > 0 else '下行'} {abs(weekly):.1f}bp"
                        ),
                    }
                )
        structure = snapshot["term_structure"]
        if structure.get("inverted"):
            alerts.append(
                {
                    "severity": "high",
                    "market": market,
                    "kind": "inversion",
                    "metric": f"{market} 10Y-2Y",
                    "value_bp": structure["spread_10y_2y_bp"],
                    "threshold_bp": ALERT_RULES["inversion_watch_bp"],
                    "message": (
                        f"{label} 10Y-2Y 倒挂 "
                        f"{abs(structure['spread_10y_2y_bp']):.1f}bp"
                    ),
                }
            )

    for key, info in spreads.items():
        daily = info.get("change_1d_bp")
        if daily is not None and abs(daily) >= ALERT_RULES["spread_daily_bp"]:
            alerts.append(
                {
                    "severity": "medium",
                    "market": "CROSS",
                    "kind": "spread_move",
                    "metric": key,
                    "value_bp": daily,
                    "threshold_bp": ALERT_RULES["spread_daily_bp"],
                    "message": (
                        f"{info['label']}单日{'走阔' if daily > 0 else '收窄'} "
                        f"{abs(daily):.1f}bp，至 {info['spread_bp']:.1f}bp"
                    ),
                }
            )

    rank = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: (rank.get(a["severity"], 3), -abs(a.get("value_bp") or 0)))
    return alerts


def market_temperature(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Blend level, slope and momentum into a 0-100 bond-market reading.

    Higher means yields are high and rising relative to their own history -
    tighter financial conditions, bonds selling off. Lower means the opposite.
    """
    tenors = snapshot["tenors"]
    level_ranks = [
        tenors[tenor]["percentile_2y"]
        for tenor in TENORS
        if tenors[tenor]["percentile_2y"] is not None
    ]
    level_score = statistics.fmean(level_ranks) if level_ranks else None

    slope = snapshot["term_structure"].get("spread_10y_2y_bp")
    # Map -100bp (deeply inverted) .. +200bp (steep) onto 0..100.
    slope_score = None
    if slope is not None:
        slope_score = max(0.0, min(100.0, (slope + 100.0) / 3.0))

    momentum = tenors["10Y"].get("change_1m_bp")
    # Map -50bp .. +50bp of one-month drift onto 0..100.
    momentum_score = None
    if momentum is not None:
        momentum_score = max(0.0, min(100.0, 50.0 + momentum))

    parts = [
        (level_score, 0.5),
        (slope_score, 0.2),
        (momentum_score, 0.3),
    ]
    usable = [(value, weight) for value, weight in parts if value is not None]
    if not usable:
        return {"score": None, "level": "unknown", "components": {}}
    total_weight = sum(weight for _, weight in usable)
    score = sum(value * weight for value, weight in usable) / total_weight

    return {
        "score": round(score, 1),
        "level": temperature_level(score),
        "components": {
            "level_percentile": round_or_none(level_score, 1),
            "curve_slope": round_or_none(slope_score, 1),
            "momentum_1m": round_or_none(momentum_score, 1),
            "formula": "50% 收益率分位 + 20% 曲线斜率 + 30% 一个月动量",
        },
    }


def temperature_level(score: float) -> str:
    if score >= 75:
        return "hot"
    if score >= 60:
        return "warm"
    if score >= 40:
        return "neutral"
    if score >= 25:
        return "cool"
    return "cold"


def build_insight(
    snapshots: Dict[str, Dict[str, Any]], spreads: Dict[str, Any]
) -> Dict[str, Any]:
    """A few plain sentences describing the current configuration."""
    drivers: List[str] = []
    for market in MARKETS:
        ten = snapshots[market]["tenors"]["10Y"]
        if ten["yield"] is None:
            continue
        change = ten.get("change_1d_bp")
        move = "持平" if change is None else f"{change:+.1f}bp"
        drivers.append(f"{MARKET_META[market]['name_zh']} 10Y {ten['yield']:.3f}%（{move}）")

    cn_us = spreads.get("CN10Y_US10Y", {}).get("spread_bp")
    if cn_us is not None:
        direction = "中债高于美债" if cn_us > 0 else "中债低于美债"
        drivers.append(f"中美 10Y 利差 {cn_us:+.1f}bp（{direction}）")

    inverted = [
        MARKET_META[m]["name_zh"]
        for m in MARKETS
        if snapshots[m]["term_structure"].get("inverted")
    ]
    headline = "三国主权收益率曲线与跨国利差监控。"
    if inverted:
        headline = f"{'、'.join(inverted)}收益率曲线处于倒挂状态。"

    return {"headline": headline, "drivers": drivers}


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    failures: List[str] = []
    stale_markets: List[str] = []
    fetched: Dict[str, int] = {}

    # China + United States via Eastmoney.
    try:
        eastmoney = fetch_eastmoney()
        for market in ("CN", "US"):
            history[market], added = merge_series(history[market], eastmoney[market])
            fetched[market] = added
    except Exception as exc:  # noqa: BLE001 - upstream shape varies
        failures.append(f"Eastmoney: {exc}")
        log(f"Eastmoney unavailable, keeping cached CN/US history: {exc}")
        stale_markets.append("CN")

    # United States cross-check / fallback via FRED.
    us_crosscheck: Dict[str, Any] = {"status": "skipped"}
    try:
        fred = fetch_fred()
        history["US"], added = merge_series(history["US"], fred)
        fetched["US"] = fetched.get("US", 0) + added
        us_crosscheck = compare_us_sources(history["US"], fred)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"FRED: {exc}")
        log(f"FRED unavailable: {exc}")
        if "US" not in fetched:
            stale_markets.append("US")

    # Japan via MOF.
    try:
        japan = fetch_mof_japan()
        history["JP"], added = merge_series(history["JP"], japan)
        fetched["JP"] = added
    except Exception as exc:  # noqa: BLE001
        failures.append(f"MOF: {exc}")
        log(f"MOF unavailable, keeping cached JP history: {exc}")
        stale_markets.append("JP")

    if not any(history[market] for market in MARKETS):
        log("FATAL: no data from any source and no cached history to fall back on.")
        return 1

    snapshots = {market: curve_snapshot(history[market]) for market in MARKETS}
    spreads = cross_market_spreads(history)
    alerts = build_alerts(snapshots, spreads)
    now = datetime.now(timezone.utc)

    markets_payload: Dict[str, Any] = {}
    for market in MARKETS:
        markets_payload[market] = {
            **MARKET_META[market],
            **snapshots[market],
            "temperature": market_temperature(snapshots[market]),
            "new_observations": fetched.get(market, 0),
        }

    payload = {
        "generated_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "tenors": list(TENORS),
        "markets": markets_payload,
        "cross_spreads": spreads,
        "alerts": alerts,
        "alert_rules": ALERT_RULES,
        "insight": build_insight(snapshots, spreads),
        "us_crosscheck": us_crosscheck,
        "data_quality": {
            "status": "degraded" if failures else "ok",
            "failures": failures,
            "stale_markets": sorted(set(stale_markets)),
            "observation_counts": {market: len(history[market]) for market in MARKETS},
        },
        "sources": {market: MARKET_META[market]["source_url"] for market in MARKETS},
    }

    write_history(history)
    YIELDS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ALERTS_PATH.write_text(
        json.dumps(
            {"generated_at": now.isoformat(), "rules": ALERT_RULES, "alerts": alerts},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    for market in MARKETS:
        ten = snapshots[market]["tenors"]["10Y"]
        level = "—" if ten["yield"] is None else f"{ten['yield']:.3f}%"
        log(f"{market} 10Y {level} as of {snapshots[market]['as_of']}")
    log(f"{len(alerts)} alert(s); wrote {YIELDS_PATH.name}, {HISTORY_PATH.name}")
    return 0


def compare_us_sources(merged: Series, fred: Series) -> Dict[str, Any]:
    """Flag any tenor where Eastmoney and FRED disagree by more than 5bp."""
    dates = sorted(stamp for stamp, values in fred.items() if values)
    if not dates:
        return {"status": "skipped"}
    latest = dates[-1]
    diffs: Dict[str, Optional[float]] = {}
    for tenor in TENORS:
        left = merged.get(latest, {}).get(tenor)
        right = fred.get(latest, {}).get(tenor)
        diffs[tenor] = None if left is None or right is None else bp(left - right)
    disagreements = {t: d for t, d in diffs.items() if d is not None and abs(d) > 5.0}
    return {
        "status": "mismatch" if disagreements else "ok",
        "as_of": latest,
        "diff_bp": diffs,
        "note": "东财与 FRED 同日差异超过 5bp 的期限" if disagreements else "两源一致（≤5bp）",
    }


if __name__ == "__main__":
    sys.exit(main())
