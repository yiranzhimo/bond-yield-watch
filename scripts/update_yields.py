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
# Backfill walks history in large pages; a daily run takes one small page.
# 60 rows is roughly three trading months - ample overlap to absorb upstream
# revisions, and still well above the 20 comparable rows the mapping guard
# needs to validate the tenor field ids on every run.
EASTMONEY_BACKFILL_PAGE_SIZE = 500
EASTMONEY_INCREMENTAL_PAGE_SIZE = 60
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

# Alerts fire on moves that are unusual *for that market*, not on a single
# basis-point number applied to all three. The three markets differ by an order
# of magnitude in daily volatility - China's 10Y often moves under 1bp a day
# while Japan's runs several bp - so a shared absolute threshold makes the China
# leg silent and the Japan leg constant noise. Each move is therefore divided by
# the trailing standard deviation of that series' own daily changes.
#
# Absolute floors keep a quiet market from alerting on statistical trivia (a
# 2-sigma move is meaningless if sigma is 0.3bp), and absolute ceilings still
# fire when a move is large in outright terms no matter how volatile the market
# has been.
ALERT_RULES = {
    "sigma_window_days": 60,
    "daily_sigma": 2.0,
    "daily_sigma_high": 3.0,
    "weekly_sigma": 2.5,
    "spread_daily_sigma": 2.0,
    "min_daily_bp": 2.0,
    "min_weekly_bp": 5.0,
    "abs_daily_bp": 15.0,
    "abs_weekly_bp": 35.0,
    "inversion_watch_bp": 0.0,
    # Staleness watch. Degrading to cached history keeps the dashboard readable
    # when a source breaks, but it makes failure look like calm: the page still
    # renders and the email still arrives, just with yesterday's numbers
    # forever. These thresholds turn silent staleness into a visible alert.
    "stale_warn_days": 4,
    "stale_high_days": 8,
}

# Rolling window used to convert a level into a percentile. Two years of
# business days keeps the score responsive without over-reacting to one month.
PERCENTILE_WINDOW_DAYS = 504
HISTORY_RETENTION_DAYS = 3650

# A single-day jump beyond this is treated as an upstream data error rather than
# a market move: no sovereign curve of these three repriced this hard in one
# session outside of a redenomination. Bad data is worse than missing data
# because it silently poisons percentiles and change calculations.
SANITY_MAX_DAILY_BP = 100.0
# Tolerance widens as sqrt(elapsed days) but never past this, so a genuinely
# corrupt print cannot slip through just because it follows a long gap.
SANITY_MAX_GAP_JUMP_BP = 400.0
SANITY_MIN_YIELD = -2.0
SANITY_MAX_YIELD = 25.0

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


def fetch_eastmoney(
    max_pages: int = 20, page_size: int = EASTMONEY_BACKFILL_PAGE_SIZE
) -> Dict[str, Series]:
    """China + United States curves from the Eastmoney datacenter feed.

    ``max_pages`` bounds the walk backwards through history. Daily runs only
    need the newest page because everything older already sits in the committed
    history; the full walk is reserved for a cold start or a detected gap.

    ``page_size`` differs by mode on purpose. Backfill wants large pages so the
    ~9.3k row history takes 19 requests instead of 156. A daily run wants a
    small page: it only needs enough overlap to pick up upstream revisions, and
    a 500-row page spans nearly two years of history that is already cached.
    """
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


def fetch_mof_japan(include_history: bool = True) -> Series:
    """JGB curve from MOF: long history overlaid with the current month.

    The history file is authoritative for old dates but trails the calendar, so
    the current-month file is applied last and wins on any overlap. Daily runs
    pass ``include_history=False`` and fetch only the small current-month file,
    skipping a 1.2 MB download whose contents are already in the cache.
    """
    out: Series = {}
    errors: List[str] = []
    sources = (
        (("history", MOF_HISTORY_URL), ("current", MOF_CURRENT_URL))
        if include_history
        else (("current", MOF_CURRENT_URL),)
    )
    for label, url in sources:
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


def fetch_fred(since: Optional[str] = None) -> Series:
    """US constant-maturity yields straight from FRED, one CSV per tenor.

    ``since`` trims the request server-side via ``cosd``, so a daily run pulls a
    handful of rows instead of the full series back to 1962.
    """
    out: Series = {}
    for tenor, series in FRED_SERIES.items():
        url = FRED_URL.format(series=series)
        if since:
            url = f"{url}&cosd={since}"
        raw = fetch_bytes(url, browser_ua=False)
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


def merge_series(
    base: Series, incoming: Series, label: str = ""
) -> Tuple[Series, int, List[str]]:
    """Overlay fresh observations onto the cache, screening out bad values.

    Returns (merged, count of new dates, rejection notes). Values that fail the
    sanity screen are dropped rather than merged: a corrupt print would
    otherwise persist in the committed history and skew every percentile and
    change calculation derived from it.
    """
    merged: Series = {stamp: dict(values) for stamp, values in base.items()}
    added = 0
    rejected: List[str] = []

    for stamp in sorted(incoming):
        values = incoming[stamp]
        clean: Dict[str, float] = {}
        for tenor, value in values.items():
            if not SANITY_MIN_YIELD <= value <= SANITY_MAX_YIELD:
                rejected.append(f"{label}{stamp} {tenor}={value} 超出合理区间")
                continue
            # An exact zero is a feed sentinel, not a yield. It must be caught
            # before the gap-scaled jump check, whose widened tolerance would
            # otherwise wave it through after a weekend or holiday.
            if value == 0.0:
                rejected.append(f"{label}{stamp} {tenor}=0 视为缺失哨兵值")
                continue
            prior = nearest_prior_observation(merged, stamp, tenor)
            if prior is not None:
                prior_stamp, reference = prior
                jump = abs(bp(value - reference) or 0.0)
                # The budget grows with the gap: a value 200 days after the last
                # print is not suspicious for moving 150bp. A flat per-jump cap
                # rejected 1716 legitimate values from the sparse early history,
                # where consecutive observations sat months apart.
                gap_days = max(
                    1, (date.fromisoformat(stamp) - date.fromisoformat(prior_stamp)).days
                )
                budget = min(
                    SANITY_MAX_DAILY_BP * math.sqrt(gap_days), SANITY_MAX_GAP_JUMP_BP
                )
                if jump > budget:
                    rejected.append(
                        f"{label}{stamp} {tenor}={value} 相对 {prior_stamp} "
                        f"跳变 {jump:.0f}bp（{gap_days} 天内上限 {budget:.0f}bp）"
                    )
                    continue
            clean[tenor] = value
        if not clean:
            continue
        if stamp not in merged:
            added += 1
            merged[stamp] = clean
        else:
            merged[stamp].update(clean)
    return merged, added, rejected


def nearest_prior_observation(
    series: Series, stamp: str, tenor: str
) -> Optional[Tuple[str, float]]:
    """Latest (date, value) strictly before ``stamp`` for one tenor, if any.

    The date is returned alongside the value so the caller can size its
    tolerance by how much time actually elapsed.
    """
    candidates = [
        other
        for other, values in series.items()
        if other < stamp and values.get(tenor) is not None
    ]
    if not candidates:
        return None
    chosen = max(candidates)
    return chosen, series[chosen][tenor]


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


def last_gap_days(series: Series, tenor: str) -> Optional[int]:
    """Calendar days spanned by the most recent observation-to-observation step.

    The "1 day" change compares consecutive *observations*, so after a holiday
    week it really spans several days. Surfacing the true gap keeps the label
    honest instead of calling a 7-day move a daily one.
    """
    dates = sorted_dates(series, tenor)
    if len(dates) < 2:
        return None
    return (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[-2])).days


def change_sigma(
    series: Series,
    tenor: str,
    step: int = 1,
    window: Optional[int] = None,
) -> Optional[float]:
    """Standard deviation of recent ``step``-observation changes, in basis points.

    This is what makes a move comparable across markets: 3bp is a big day for
    Chinese government bonds and a quiet one for JGBs right now, and dividing by
    each market's own sigma puts them on the same footing.

    ``step`` is measured in observations, so weekly volatility is sampled
    directly from realised 5-observation changes rather than scaled from the
    daily figure by sqrt(5). That scaling assumes successive days are
    independent, which fails in a trending market - JGB yields are up 134bp over
    a year - and understates weekly volatility whenever moves autocorrelate,
    making a nominal "2.5 sigma" weekly trigger fire more often than intended.

    ``window`` defaults to ALERT_RULES at call time rather than at import, so
    tuning the rules dict actually takes effect.
    """
    if step < 1:
        return None
    if window is None:
        window = ALERT_RULES["sigma_window_days"]

    dates = sorted_dates(series, tenor)
    if len(dates) < max(12, step + 2):
        return None
    recent = dates[-(window + step):]
    diffs: List[float] = []
    for earlier, later in zip(recent, recent[step:]):
        left = value_on(series, earlier, tenor)
        right = value_on(series, later, tenor)
        if left is None or right is None:
            continue
        move = bp(right - left)
        if move is not None:
            diffs.append(move)
    if len(diffs) < 10:
        return None
    try:
        sigma = statistics.stdev(diffs)
    except statistics.StatisticsError:
        return None
    return None if sigma <= 0 else round(sigma, 2)


def daily_change_sigma(series: Series, tenor: str) -> Optional[float]:
    """Daily-change volatility, in basis points."""
    return change_sigma(series, tenor, step=1)


def weekly_change_sigma(series: Series, tenor: str) -> Optional[float]:
    """Weekly-change volatility measured over 5 observations, in basis points."""
    return change_sigma(series, tenor, step=5)


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
            "daily_sigma_bp": daily_change_sigma(series, tenor),
            "weekly_sigma_bp": weekly_change_sigma(series, tenor),
            "last_gap_days": last_gap_days(series, tenor),
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
        "history_curves": historical_curves(series, latest),
        "ten_year_history": tenor_series_points(series, "10Y"),
        "term_structure": {
            "spread_10y_2y_bp": spread_10_2,
            "spread_30y_10y_bp": spread_30_10,
            "spread_10y_5y_bp": spread_10_5,
            "shape": curve_shape(spread_10_2, spread_30_10),
            "inverted": bool(spread_10_2 is not None and spread_10_2 < 0),
        },
    }


def historical_curves(series: Series, latest: Optional[str]) -> Dict[str, Any]:
    """The curve as it stood a month and a year ago, for overlay comparison.

    Reading how the whole curve shifted is what bond analysis actually turns on -
    a parallel shift, a steepening and a twist can all leave the 10Y unchanged.
    """
    out: Dict[str, Any] = {}
    if not latest:
        return out
    for key, days in (("1m", 30), ("1y", 365)):
        target = (date.fromisoformat(latest) - timedelta(days=days)).isoformat()
        curve: Dict[str, Optional[float]] = {}
        stamps: List[str] = []
        for tenor in TENORS:
            dates = sorted_dates(series, tenor)
            chosen: Optional[str] = None
            for stamp in dates:
                if stamp <= target:
                    chosen = stamp
                else:
                    break
            curve[tenor] = round_or_none(value_on(series, chosen, tenor)) if chosen else None
            if chosen:
                stamps.append(chosen)
        if any(value is not None for value in curve.values()):
            out[key] = {"as_of": max(stamps) if stamps else None, "tenors": curve}
    return out


def tenor_series_points(series: Series, tenor: str, limit: int = 520) -> List[List[Any]]:
    """Compact [date, yield] pairs so the page can chart one tenor's path."""
    dates = sorted_dates(series, tenor)[-limit:]
    return [[stamp, round_or_none(value_on(series, stamp, tenor))] for stamp in dates]


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
            "daily_sigma_bp": daily_change_sigma(series, tenor),
            "history": spread_series_points(series, tenor),
        }
    return out


def spread_series_points(series: Series, tenor: str, limit: int = 520) -> List[List[Any]]:
    """Compact [date, bp] pairs for charting a spread's recent path."""
    dates = sorted_dates(series, tenor)[-limit:]
    points: List[List[Any]] = []
    for stamp in dates:
        value = bp(value_on(series, stamp, tenor))
        if value is not None:
            points.append([stamp, value])
    return points


def judge_move(
    move_bp: Optional[float],
    sigma_bp: Optional[float],
    *,
    sigma_threshold: float,
    floor_bp: float,
    ceiling_bp: float,
    high_sigma: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Decide whether one move deserves an alert, and how loud.

    Two independent paths can trigger: the move is large relative to the
    market's own recent volatility (and clears an absolute floor, so a
    near-motionless market does not alert on statistical noise), or the move is
    simply large outright regardless of volatility. Returns None when neither
    applies.
    """
    move = finite(move_bp)
    if move is None:
        return None
    magnitude = abs(move)

    multiple: Optional[float] = None
    sigma = finite(sigma_bp)
    if sigma and sigma > 0:
        multiple = round(magnitude / sigma, 1)

    by_sigma = (
        multiple is not None and multiple >= sigma_threshold and magnitude >= floor_bp
    )
    by_absolute = magnitude >= ceiling_bp
    if not (by_sigma or by_absolute):
        return None

    severity = "medium"
    if by_absolute or (high_sigma is not None and multiple is not None and multiple >= high_sigma):
        severity = "high"

    if by_sigma and multiple is not None:
        suffix = f"（{multiple:.1f}σ）"
        trigger = "sigma"
    else:
        suffix = "（绝对阈值）"
        trigger = "absolute"
    if by_sigma and by_absolute and multiple is not None:
        suffix = f"（{multiple:.1f}σ，超绝对阈值）"
        trigger = "both"

    return {"severity": severity, "multiple": multiple, "trigger": trigger, "suffix": suffix}


def staleness_alert(
    market: str, snapshot: Dict[str, Any], label: str, today: Optional[date] = None
) -> Optional[Dict[str, Any]]:
    """Alert when a market's newest observation has stopped advancing.

    Without this the project cannot report its own failure: every source
    degrades to cached history, so a broken upstream produces a page and an
    email that look completely normal apart from a date that never moves.
    Weekends and holidays are tolerated by the day thresholds.
    """
    as_of = snapshot.get("as_of")
    if not as_of:
        return {
            "severity": "high",
            "market": market,
            "kind": "stale_data",
            "metric": f"{market} 数据",
            "value_bp": None,
            "age_days": None,
            "message": f"{label}没有任何可用观测,数据源可能已失效",
        }

    reference = today or datetime.now(timezone.utc).date()
    age = (reference - date.fromisoformat(as_of)).days
    if age < ALERT_RULES["stale_warn_days"]:
        return None
    return {
        "severity": "high" if age >= ALERT_RULES["stale_high_days"] else "medium",
        "market": market,
        "kind": "stale_data",
        "metric": f"{market} 数据",
        "value_bp": None,
        "age_days": age,
        "message": (
            f"{label}最新数据为 {as_of},已 {age} 天未更新,"
            "疑似数据源中断而非市场休市"
        ),
    }


def build_alerts(
    snapshots: Dict[str, Dict[str, Any]], spreads: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Moves that are unusual for their own market, most severe first."""
    alerts: List[Dict[str, Any]] = []

    for market in MARKETS:
        snapshot = snapshots[market]
        label = MARKET_META[market]["name_zh"]
        for tenor in TENORS:
            info = snapshot["tenors"][tenor]
            sigma = info.get("daily_sigma_bp")

            daily = info.get("change_1d_bp")
            verdict = judge_move(
                daily,
                sigma,
                sigma_threshold=ALERT_RULES["daily_sigma"],
                floor_bp=ALERT_RULES["min_daily_bp"],
                ceiling_bp=ALERT_RULES["abs_daily_bp"],
                high_sigma=ALERT_RULES["daily_sigma_high"],
            )
            if verdict:
                gap = info.get("last_gap_days")
                # Consecutive observations can straddle a holiday; say so rather
                # than calling a week-long move a daily one.
                window = "单日" if not gap or gap <= 3 else f"{gap}日间"
                level = info.get("yield")
                tail = "" if level is None else f"，至 {level:.3f}%"
                alerts.append(
                    {
                        "severity": verdict["severity"],
                        "market": market,
                        "kind": "daily_move",
                        "metric": f"{market} {tenor}",
                        "value_bp": daily,
                        "sigma_bp": sigma,
                        "sigma_multiple": verdict["multiple"],
                        "trigger": verdict["trigger"],
                        "message": (
                            f"{label} {tenor} {window}{'上行' if daily > 0 else '下行'} "
                            f"{abs(daily):.1f}bp{verdict['suffix']}{tail}"
                        ),
                    }
                )

            weekly = info.get("change_1w_bp")
            # Measured 5-observation volatility, not daily sigma times sqrt(5).
            weekly_sigma = info.get("weekly_sigma_bp")
            verdict = judge_move(
                weekly,
                weekly_sigma,
                sigma_threshold=ALERT_RULES["weekly_sigma"],
                floor_bp=ALERT_RULES["min_weekly_bp"],
                ceiling_bp=ALERT_RULES["abs_weekly_bp"],
                high_sigma=None,
            )
            if verdict:
                alerts.append(
                    {
                        "severity": verdict["severity"],
                        "market": market,
                        "kind": "weekly_move",
                        "metric": f"{market} {tenor}",
                        "value_bp": weekly,
                        "sigma_bp": None if weekly_sigma is None else round(weekly_sigma, 2),
                        "sigma_multiple": verdict["multiple"],
                        "trigger": verdict["trigger"],
                        "message": (
                            f"{label} {tenor} 一周累计"
                            f"{'上行' if weekly > 0 else '下行'} {abs(weekly):.1f}bp"
                            f"{verdict['suffix']}"
                        ),
                    }
                )
        stale = staleness_alert(market, snapshot, label)
        if stale:
            alerts.append(stale)

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
        sigma = info.get("daily_sigma_bp")
        verdict = judge_move(
            daily,
            sigma,
            sigma_threshold=ALERT_RULES["spread_daily_sigma"],
            floor_bp=ALERT_RULES["min_daily_bp"],
            ceiling_bp=ALERT_RULES["abs_daily_bp"],
            high_sigma=ALERT_RULES["daily_sigma_high"],
        )
        if verdict:
            spread_level = finite(info.get("spread_bp"))
            tail = "" if spread_level is None else f"，至 {spread_level:.1f}bp"
            alerts.append(
                {
                    "severity": verdict["severity"],
                    "market": "CROSS",
                    "kind": "spread_move",
                    "metric": key,
                    "value_bp": daily,
                    "sigma_bp": sigma,
                    "sigma_multiple": verdict["multiple"],
                    "trigger": verdict["trigger"],
                    "message": (
                        f"{info['label']}单日{'走阔' if daily > 0 else '收窄'} "
                        f"{abs(daily):.1f}bp{verdict['suffix']}{tail}"
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


def needs_backfill(history: Dict[str, Series], min_days: int = 400) -> Tuple[bool, str]:
    """Decide between a cheap incremental refresh and a full history walk.

    A full walk is only needed on a cold start, when a market is thin enough
    that percentiles would be unreliable, or when the cache has fallen far
    enough behind that an incremental window might not close the gap.
    """
    today = datetime.now(timezone.utc).date()
    for market in MARKETS:
        series = history[market]
        if not series:
            return True, f"{market} 无缓存历史"
        if len(series) < min_days:
            return True, f"{market} 仅 {len(series)} 个观测，不足 {min_days}"
        lag = (today - date.fromisoformat(max(series))).days
        if lag > 20:
            return True, f"{market} 缓存落后 {lag} 天"
    return False, "缓存充足，仅取增量"


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    failures: List[str] = []
    stale_markets: List[str] = []
    rejections: List[str] = []
    fetched: Dict[str, int] = {}

    backfill, reason = needs_backfill(history)
    log(f"Fetch mode: {'full backfill' if backfill else 'incremental'} ({reason})")
    # Incremental runs re-read a short overlap window rather than only the newest
    # print, so a revised or late-published observation still gets corrected.
    fred_since = (
        None
        if backfill
        else (datetime.now(timezone.utc).date() - timedelta(days=45)).isoformat()
    )

    # China + United States via Eastmoney. The raw US series is kept aside so
    # the cross-check can compare two independent sources rather than the merged
    # result, which would already carry FRED's own values.
    eastmoney_us: Series = {}
    try:
        eastmoney = fetch_eastmoney(
            max_pages=20 if backfill else 1,
            page_size=(
                EASTMONEY_BACKFILL_PAGE_SIZE
                if backfill
                else EASTMONEY_INCREMENTAL_PAGE_SIZE
            ),
        )
        eastmoney_us = eastmoney["US"]
        for market in ("CN", "US"):
            history[market], added, bad = merge_series(
                history[market], eastmoney[market], label=f"东财 {market} "
            )
            fetched[market] = added
            rejections.extend(bad)
    except Exception as exc:  # noqa: BLE001 - upstream shape varies
        failures.append(f"Eastmoney: {exc}")
        log(f"Eastmoney unavailable, keeping cached CN/US history: {exc}")
        stale_markets.append("CN")

    # United States cross-check / fallback via FRED.
    us_crosscheck: Dict[str, Any] = {"status": "skipped"}
    try:
        fred = fetch_fred(since=fred_since)
        history["US"], added, bad = merge_series(history["US"], fred, label="FRED ")
        fetched["US"] = fetched.get("US", 0) + added
        rejections.extend(bad)
        us_crosscheck = compare_us_sources(eastmoney_us, fred)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"FRED: {exc}")
        log(f"FRED unavailable: {exc}")
        if "US" not in fetched:
            stale_markets.append("US")

    # Japan via MOF.
    try:
        japan = fetch_mof_japan(include_history=backfill)
        history["JP"], added, bad = merge_series(history["JP"], japan, label="MOF ")
        fetched["JP"] = added
        rejections.extend(bad)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"MOF: {exc}")
        log(f"MOF unavailable, keeping cached JP history: {exc}")
        stale_markets.append("JP")

    for note in rejections[:20]:
        log(f"Rejected by sanity screen: {note}")

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
            # A stale-data alert means the numbers stopped moving even if every
            # fetch returned 200, so it counts as degraded too.
            "status": "degraded"
            if failures or any(a["kind"] == "stale_data" for a in alerts)
            else "ok",
            "failures": failures,
            "stale_markets": sorted(set(stale_markets)),
            "observation_counts": {market: len(history[market]) for market in MARKETS},
            "fetch_mode": "backfill" if backfill else "incremental",
            "fetch_mode_reason": reason,
            "rejected_values": rejections[:50],
            "rejected_count": len(rejections),
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


def compare_us_sources(eastmoney: Series, fred: Series) -> Dict[str, Any]:
    """Flag any tenor where Eastmoney and FRED disagree by more than 5bp.

    Both arguments must be the *raw* per-source series. Passing the merged
    history here would compare FRED against itself, since merging overwrites
    the Eastmoney value with FRED's - a check that can only ever report 0.0bp.

    The two feeds also publish on different lags, so the comparison is made on
    the newest date they share rather than on either one's own latest date.
    """
    shared = sorted(
        stamp
        for stamp, values in fred.items()
        if values and any(eastmoney.get(stamp, {}).get(t) is not None for t in TENORS)
    )
    if not shared:
        return {
            "status": "no_overlap",
            "note": "两源暂无共同交易日可比对（通常是发布时滞所致）",
        }

    latest = shared[-1]
    diffs: Dict[str, Optional[float]] = {}
    for tenor in TENORS:
        left = eastmoney.get(latest, {}).get(tenor)
        right = fred.get(latest, {}).get(tenor)
        diffs[tenor] = None if left is None or right is None else bp(left - right)

    comparable = {t: d for t, d in diffs.items() if d is not None}
    if not comparable:
        return {
            "status": "no_overlap",
            "as_of": latest,
            "diff_bp": diffs,
            "note": "共同交易日上没有可比对的期限",
        }

    disagreements = {t: d for t, d in comparable.items() if abs(d) > 5.0}
    worst = max(abs(d) for d in comparable.values())
    return {
        "status": "mismatch" if disagreements else "ok",
        "as_of": latest,
        "diff_bp": diffs,
        "max_abs_diff_bp": round(worst, 1),
        "note": (
            f"东财与 FRED 在 {latest} 有 {len(disagreements)} 个期限差异超过 5bp"
            if disagreements
            else f"两源在 {latest} 一致（最大差异 {worst:.1f}bp）"
        ),
    }


if __name__ == "__main__":
    sys.exit(main())
