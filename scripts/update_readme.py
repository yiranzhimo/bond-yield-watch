#!/usr/bin/env python3
"""Refresh the live-snapshot block inside README.md from data/yields.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
YIELDS_PATH = ROOT / "data" / "yields.json"
README_PATH = ROOT / "README.md"

START = "<!-- SNAPSHOT:START -->"
END = "<!-- SNAPSHOT:END -->"

TENORS = ("2Y", "5Y", "10Y", "30Y")
MARKETS = ("CN", "US", "JP")
SHAPE_ZH = {
    "steep": "陡峭",
    "normal": "正常",
    "flat": "平坦",
    "inverted": "倒挂",
    "deeply_inverted": "深度倒挂",
    "unknown": "—",
}


def finite(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def pct(value: Any) -> str:
    parsed = finite(value)
    return "—" if parsed is None else f"{parsed:.3f}%"


def signed_bp(value: Any) -> str:
    parsed = finite(value)
    return "—" if parsed is None else f"{parsed:+.1f}bp"


def plain_bp(value: Any) -> str:
    parsed = finite(value)
    return "—" if parsed is None else f"{parsed:.1f}bp"


def build_block(data: Dict[str, Any]) -> str:
    lines: List[str] = [START, ""]
    as_of = data.get("markets", {}).get("US", {}).get("as_of") or "—"
    lines.append(f"**数据日期 {as_of}** · 更新于 {data.get('updated_at', '—')[:19]} UTC")
    lines.append("")
    lines.append("| 市场 | 2Y | 5Y | 10Y | 30Y | 10Y 1日 | 10Y-2Y | 曲线 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for market in MARKETS:
        payload = data.get("markets", {}).get(market)
        if not payload:
            continue
        tenors = payload.get("tenors", {})
        structure = payload.get("term_structure", {})
        cells = [payload.get("name_zh", market)]
        cells += [pct(tenors.get(tenor, {}).get("yield")) for tenor in TENORS]
        cells.append(signed_bp(tenors.get("10Y", {}).get("change_1d_bp")))
        cells.append(plain_bp(structure.get("spread_10y_2y_bp")))
        cells.append(SHAPE_ZH.get(structure.get("shape"), "—"))
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("**跨国利差**")
    lines.append("")
    for info in (data.get("cross_spreads") or {}).values():
        percentile = finite(info.get("percentile_2y"))
        tail = "" if percentile is None else f"，两年分位 {percentile:.0f}"
        lines.append(
            f"- {info.get('label', '')}：{plain_bp(info.get('spread_bp'))}"
            f"（1日 {signed_bp(info.get('change_1d_bp'))}{tail}）"
        )

    alerts = data.get("alerts") or []
    lines.append("")
    if alerts:
        lines.append(f"**异动告警（{len(alerts)} 条）**")
        lines.append("")
        for alert in alerts[:8]:
            lines.append(f"- `{alert.get('severity')}` {alert.get('message')}")
    else:
        lines.append("**异动告警**：当前无触发项。")

    quality = data.get("data_quality") or {}
    if quality.get("status") != "ok":
        lines.append("")
        lines.append(f"> 数据源降级：{'；'.join(quality.get('failures', []))}")

    lines.extend(["", END])
    return "\n".join(lines)


def main() -> int:
    if not YIELDS_PATH.exists():
        print(f"{YIELDS_PATH} missing; run scripts/update_yields.py first.", file=sys.stderr)
        return 1
    if not README_PATH.exists():
        print(f"{README_PATH} missing.", file=sys.stderr)
        return 1

    data = json.loads(YIELDS_PATH.read_text(encoding="utf-8"))
    readme = README_PATH.read_text(encoding="utf-8")
    block = build_block(data)

    if START in readme and END in readme:
        head, _, rest = readme.partition(START)
        _, _, tail = rest.partition(END)
        updated = head + block + tail
    else:
        updated = readme.rstrip() + "\n\n" + block + "\n"

    if updated != readme:
        README_PATH.write_text(updated, encoding="utf-8")
        print("README snapshot updated.")
    else:
        print("README snapshot unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
