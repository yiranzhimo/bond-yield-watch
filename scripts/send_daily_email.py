#!/usr/bin/env python3
"""Send the refreshed sovereign-yield snapshot as a compact daily SMTP email."""

from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
YIELDS_PATH = ROOT / "data" / "yields.json"
DEFAULT_SITE_URL = "https://example.github.io/bond-yield-watch/"

TENORS = ("2Y", "5Y", "10Y", "30Y")
MARKETS = ("CN", "US", "JP")

TEMP_ZH = {
    "hot": "偏热",
    "warm": "偏暖",
    "neutral": "中性",
    "cool": "偏冷",
    "cold": "偏冷",
    "unknown": "—",
}
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


def pct(value: Any, digits: int = 3) -> str:
    parsed = finite(value)
    return "—" if parsed is None else f"{parsed:.{digits}f}%"


def signed_bp(value: Any) -> str:
    parsed = finite(value)
    return "—" if parsed is None else f"{parsed:+.1f}bp"


def plain_bp(value: Any) -> str:
    parsed = finite(value)
    return "—" if parsed is None else f"{parsed:.1f}bp"


def tone(value: Any) -> str:
    parsed = finite(value)
    if parsed is None or parsed == 0:
        return "#6d706b"
    return "#b64634" if parsed > 0 else "#1f6a4d"


def build_subject(data: Dict[str, Any]) -> str:
    parts: List[str] = []
    for market in MARKETS:
        payload = data.get("markets", {}).get(market, {})
        ten = payload.get("tenors", {}).get("10Y", {})
        level = finite(ten.get("yield"))
        change = finite(ten.get("change_1d_bp"))
        if level is None:
            continue
        label = {"CN": "中", "US": "美", "JP": "日"}[market]
        suffix = "" if change is None else f"{change:+.0f}bp"
        parts.append(f"{label}{level:.2f}%{suffix}")
    alerts = data.get("alerts") or []
    head = "国债日报"
    if alerts:
        head = f"国债日报 · {len(alerts)}条异动"
    as_of = data.get("markets", {}).get("US", {}).get("as_of") or ""
    return f"[{as_of}] {head} | " + " ".join(parts) if parts else f"[{as_of}] {head}"


def render_html(data: Dict[str, Any], site_url: str) -> str:
    rows: List[str] = []
    for market in MARKETS:
        payload = data.get("markets", {}).get(market)
        if not payload:
            continue
        tenors = payload.get("tenors", {})
        structure = payload.get("term_structure", {})
        temperature = payload.get("temperature", {})
        score = finite(temperature.get("score"))
        score_text = "—" if score is None else f"{score:.1f} {TEMP_ZH.get(temperature.get('level'), '')}"
        rows.append(
            f"""
        <tr><td colspan="4" style="padding:16px 8px 6px;font-weight:600;font-size:15px;">
          {html.escape(payload.get('name_zh', market))}
          <span style="font-weight:400;color:#6d706b;font-size:12px;">
            {html.escape(payload.get('as_of') or '')} · 温度 {html.escape(score_text)}
            · 曲线{html.escape(SHAPE_ZH.get(structure.get('shape'), '—'))}
            · 10Y-2Y {html.escape(plain_bp(structure.get('spread_10y_2y_bp')))}
          </span>
        </td></tr>"""
        )
        for tenor in TENORS:
            info = tenors.get(tenor, {})
            rows.append(
                f"""
        <tr>
          <td style="padding:5px 8px;color:#6d706b;font-size:13px;">{tenor}</td>
          <td style="padding:5px 8px;text-align:right;font-family:monospace;">{pct(info.get('yield'))}</td>
          <td style="padding:5px 8px;text-align:right;font-family:monospace;color:{tone(info.get('change_1d_bp'))};">
            {signed_bp(info.get('change_1d_bp'))}</td>
          <td style="padding:5px 8px;text-align:right;font-family:monospace;color:{tone(info.get('change_1w_bp'))};">
            {signed_bp(info.get('change_1w_bp'))}</td>
        </tr>"""
            )

    spread_rows: List[str] = []
    for info in (data.get("cross_spreads") or {}).values():
        spread_rows.append(
            f"""
        <tr>
          <td style="padding:5px 8px;color:#6d706b;font-size:13px;">{html.escape(info.get('label', ''))}</td>
          <td style="padding:5px 8px;text-align:right;font-family:monospace;">{plain_bp(info.get('spread_bp'))}</td>
          <td style="padding:5px 8px;text-align:right;font-family:monospace;color:{tone(info.get('change_1d_bp'))};">
            {signed_bp(info.get('change_1d_bp'))}</td>
        </tr>"""
        )

    alerts = data.get("alerts") or []
    if alerts:
        items = "".join(
            f"""<li style="margin-bottom:6px;line-height:1.5;">
              <span style="font-family:monospace;font-size:12px;color:#6d706b;">
                {html.escape(str(a.get('metric', '')))}</span>
              &nbsp;{html.escape(str(a.get('message', '')))}</li>"""
            for a in alerts[:10]
        )
        # Name the yardstick, since the same bp move means different things in
        # each market and the sigma multiple in each line is what ranks them.
        items += (
            "<li style='margin-top:8px;font-size:11px;color:#6d706b;list-style:none;'>"
            "σ 为该期限过去 60 个交易日日变动的标准差</li>"
        )
        alert_block = f"""
      <div style="background:#faf4ec;border-left:3px solid #b57a21;padding:14px 16px;margin-bottom:22px;">
        <div style="font-size:12px;letter-spacing:.08em;color:#6d706b;margin-bottom:8px;">异动告警</div>
        <ul style="margin:0;padding-left:18px;font-size:14px;">{items}</ul>
      </div>"""
    else:
        alert_block = """
      <div style="background:#f0f4f0;border-left:3px solid #1f6a4d;padding:14px 16px;margin-bottom:22px;font-size:14px;">
        当前无触发告警的异动。
      </div>"""

    insight = data.get("insight") or {}
    drivers = "".join(
        f"<li style='margin-bottom:4px;'>{html.escape(str(d))}</li>" for d in insight.get("drivers", [])
    )
    quality = data.get("data_quality") or {}
    quality_note = ""
    if quality.get("status") != "ok":
        stale = [a for a in alerts if a.get("kind") == "stale_data"]
        notes = list(quality.get("failures", []))
        # Name staleness first: it is the case where every fetch succeeded and
        # the numbers still stopped moving, which no failure string would show.
        if stale:
            notes = [str(a.get("message", "")) for a in stale] + notes
        quality_note = (
            "<p style='font-size:12px;color:#b57a21;margin:0 0 14px;'>数据质量警示："
            f"{html.escape('；'.join(notes))}</p>"
        )

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f3f0e8;font-family:-apple-system,'Helvetica Neue',sans-serif;color:#191b1a;">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:14px;padding:28px;">
    <div style="font-size:12px;letter-spacing:.14em;color:#6d706b;margin-bottom:8px;">BOND WATCH · 中美日国债</div>
    <h1 style="font-size:22px;margin:0 0 12px;font-weight:600;">{html.escape(insight.get('headline', '国债日报'))}</h1>
    {quality_note}
    <ul style="font-size:14px;color:#3d403c;margin:0 0 22px;padding-left:18px;">{drivers}</ul>
    {alert_block}
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
      <thead><tr>
        <th style="text-align:left;padding:6px 8px;font-size:11px;color:#6d706b;font-weight:400;">期限</th>
        <th style="text-align:right;padding:6px 8px;font-size:11px;color:#6d706b;font-weight:400;">收益率</th>
        <th style="text-align:right;padding:6px 8px;font-size:11px;color:#6d706b;font-weight:400;">1日</th>
        <th style="text-align:right;padding:6px 8px;font-size:11px;color:#6d706b;font-weight:400;">1周</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    <div style="font-size:12px;letter-spacing:.08em;color:#6d706b;margin-bottom:8px;">跨国利差</div>
    <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
      <tbody>{''.join(spread_rows)}</tbody>
    </table>
    <a href="{html.escape(site_url)}" style="display:inline-block;padding:10px 18px;background:#191b1a;color:#f3f0e8;
      text-decoration:none;border-radius:999px;font-size:13px;">查看完整看板</a>
    <p style="font-size:11px;color:#6d706b;margin:22px 0 0;line-height:1.6;">
      数据来源：东方财富数据中心（中/美）、日本财务省 MOF（日）、FRED（美债校验）。仅供研究参考，不构成投资建议。
    </p>
  </div>
</body></html>"""


def render_text(data: Dict[str, Any], site_url: str) -> str:
    lines: List[str] = [(data.get("insight") or {}).get("headline", "国债日报"), ""]
    for driver in (data.get("insight") or {}).get("drivers", []):
        lines.append(f"- {driver}")
    lines.append("")
    for market in MARKETS:
        payload = data.get("markets", {}).get(market)
        if not payload:
            continue
        structure = payload.get("term_structure", {})
        lines.append(
            f"{payload.get('name_zh', market)}（{payload.get('as_of') or '—'}）"
            f" 10Y-2Y {plain_bp(structure.get('spread_10y_2y_bp'))}"
            f" 曲线{SHAPE_ZH.get(structure.get('shape'), '—')}"
        )
        for tenor in TENORS:
            info = payload.get("tenors", {}).get(tenor, {})
            lines.append(
                f"  {tenor:>3} {pct(info.get('yield')):>9}"
                f"  1日 {signed_bp(info.get('change_1d_bp')):>9}"
                f"  1周 {signed_bp(info.get('change_1w_bp')):>9}"
                f"  日σ {plain_bp(info.get('daily_sigma_bp')):>7}"
            )
        lines.append("")
    lines.append("跨国利差：")
    for info in (data.get("cross_spreads") or {}).values():
        lines.append(
            f"  {info.get('label', '')} {plain_bp(info.get('spread_bp'))}"
            f"（1日 {signed_bp(info.get('change_1d_bp'))}）"
        )
    alerts = data.get("alerts") or []
    lines.append("")
    if alerts:
        lines.append(f"异动告警（{len(alerts)} 条）：")
        for alert in alerts[:10]:
            lines.append(f"  [{alert.get('severity')}] {alert.get('message')}")
    else:
        lines.append("异动告警：无")
    lines.extend(["", f"完整看板：{site_url}", "仅供研究参考，不构成投资建议。"])
    return "\n".join(lines)


def env_list(name: str) -> List[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Render locally without sending.")
    parser.add_argument("--out", type=Path, help="With --dry-run, write the HTML here.")
    parser.add_argument(
        "--only-on-alert",
        action="store_true",
        help="Skip sending when there are no active alerts.",
    )
    args = parser.parse_args()

    if not YIELDS_PATH.exists():
        print(f"{YIELDS_PATH} missing; run scripts/update_yields.py first.", file=sys.stderr)
        return 1
    data = json.loads(YIELDS_PATH.read_text(encoding="utf-8"))

    site_url = os.environ.get("SITE_URL") or DEFAULT_SITE_URL
    subject = build_subject(data)
    body_html = render_html(data, site_url)
    body_text = render_text(data, site_url)

    if args.only_on_alert and not (data.get("alerts") or []):
        # An empty alert list here means the pipeline ran and found a quiet
        # market. A broken pipeline shows up as a stale_data alert instead, so
        # it still gets delivered rather than being mistaken for a calm day.
        print("No alerts today; skipping email as requested.")
        return 0

    if args.dry_run:
        target = args.out or (ROOT / "email_preview.html")
        target.write_text(body_html, encoding="utf-8")
        print(f"Subject: {subject}")
        print(body_text)
        print(f"\nHTML preview written to {target}")
        return 0

    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "465") or 465)
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("EMAIL_FROM", "").strip() or username
    recipients = env_list("EMAIL_TO")

    missing = [
        name
        for name, value in (
            ("SMTP_HOST", host),
            ("SMTP_USERNAME", username),
            ("SMTP_PASSWORD", password),
            ("EMAIL_TO", recipients),
        )
        if not value
    ]
    if missing:
        print(f"Missing SMTP settings: {', '.join(missing)}", file=sys.stderr)
        return 1

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body_text)
    message.add_alternative(body_html, subtype="html")

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=45) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=45) as server:
            server.starttls(context=context)
            server.login(username, password)
            server.send_message(message)

    print(f"Sent '{subject}' to {len(recipients)} recipient(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
