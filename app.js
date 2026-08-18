const DATA_URL = "./data/yields.json";

const TENORS = ["2Y", "5Y", "10Y", "30Y"];
const MARKETS = ["CN", "US", "JP"];

const tempLabel = {
  hot: "偏热",
  warm: "偏暖",
  neutral: "中性",
  cool: "偏冷",
  cold: "偏冷",
  unknown: "—",
};

const shapeLabel = {
  steep: "陡峭",
  normal: "正常",
  flat: "平坦",
  inverted: "倒挂",
  deeply_inverted: "深度倒挂",
  unknown: "—",
};

const changeColumns = [
  ["change_1d_bp", "1日"],
  ["change_1w_bp", "1周"],
  ["change_1m_bp", "1月"],
  ["change_3m_bp", "3月"],
  ["change_1y_bp", "1年"],
];

// Daily sigma is the yardstick the alerts use, so it belongs next to the moves
// it scales - a 4bp day means different things in Beijing and Tokyo.
const SIGMA_COLUMN = ["daily_sigma_bp", "日σ"];

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function finite(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function pct(value, digits = 3) {
  const parsed = finite(value);
  return parsed === null ? "—" : `${parsed.toFixed(digits)}%`;
}

function signedBp(value, digits = 1) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  return `${parsed > 0 ? "+" : ""}${parsed.toFixed(digits)}bp`;
}

function plainBp(value, digits = 1) {
  const parsed = finite(value);
  return parsed === null ? "—" : `${parsed.toFixed(digits)}bp`;
}

function toneClass(value, invert = false) {
  const parsed = finite(value);
  if (parsed === null || parsed === 0) return "flatten";
  const rising = invert ? parsed < 0 : parsed > 0;
  return rising ? "up" : "down";
}

function dateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Hong_Kong",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function renderHero(data) {
  const insight = data.insight || {};
  const words = { hot: "偏热", warm: "偏暖", neutral: "各走各路", cool: "偏冷", cold: "偏冷" };
  const scores = MARKETS.map((m) => finite(data.markets?.[m]?.temperature?.score)).filter(
    (v) => v !== null,
  );
  const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null;
  const level = avg === null ? "unknown" : avg >= 70 ? "hot" : avg >= 55 ? "warm" : avg >= 40 ? "neutral" : "cool";
  $("#hero-word").textContent = words[level] || "各走各路";
  $("#hero-summary").textContent = insight.headline || "—";

  const list = $("#driver-list");
  list.innerHTML = "";
  (insight.drivers || []).forEach((text) => {
    const item = document.createElement("span");
    item.className = "driver-chip";
    item.textContent = text;
    list.append(item);
  });
}

function renderQuality(data) {
  const quality = data.data_quality || {};
  const state = $("#quality-state");
  const stale = (data.alerts || []).filter((a) => a.kind === "stale_data");
  const ok = quality.status === "ok";

  // Staleness is the failure mode that looks like calm, so it gets named
  // explicitly rather than folded into a generic "degraded" label.
  if (stale.length) {
    const worst = Math.max(...stale.map((a) => finite(a.age_days) ?? 0));
    state.textContent = `数据陈旧 ${worst} 天`;
  } else {
    state.textContent = ok ? "数据正常" : "部分数据源降级";
  }
  state.classList.toggle("is-degraded", !ok);
  $("#updated-at").textContent = `更新于 ${dateTime(data.updated_at)}`;
}

function renderAlerts(data) {
  const alerts = data.alerts || [];
  const badge = $("#alert-count");
  badge.textContent = alerts.length ? `${alerts.length} 条` : "无异动";
  badge.classList.toggle("is-quiet", alerts.length === 0);

  const list = $("#alert-list");
  list.innerHTML = "";
  if (!alerts.length) {
    const empty = document.createElement("li");
    empty.className = "alert-empty";
    empty.textContent = "当前无触发告警的异动。";
    list.append(empty);
    return;
  }
  alerts.slice(0, 8).forEach((alert) => {
    const item = document.createElement("li");
    item.className = `alert-item sev-${alert.severity || "medium"}`;
    const label = document.createElement("span");
    label.className = "alert-metric";
    label.textContent = alert.metric || "";
    const message = document.createElement("span");
    message.className = "alert-message";
    message.textContent = alert.message || "";
    item.append(label, message);
    list.append(item);
  });
}

function curvePoints(tenors, scale = null) {
  const values = TENORS.map((tenor) => finite(tenors?.[tenor]?.yield));
  if (values.some((value) => value === null)) return null;
  const min = scale ? scale.min : Math.min(...values);
  const max = scale ? scale.max : Math.max(...values);
  const span = max - min || 1;
  return values.map((value, index) => ({
    x: 20 + (index * 280) / (TENORS.length - 1),
    y: 100 - ((value - min) / span) * 76,
    value,
    tenor: TENORS[index],
  }));
}

function renderMarketCard(card, market, payload) {
  const tenors = payload.tenors || {};
  const structure = payload.term_structure || {};
  const temperature = payload.temperature || {};

  const chip = $('[data-role="temp"]', card);
  const score = finite(temperature.score);
  chip.textContent = score === null ? "—" : `${score.toFixed(1)} ${tempLabel[temperature.level] || ""}`;
  chip.dataset.level = temperature.level || "unknown";

  $('[data-role="ten"]', card).textContent = pct(tenors["10Y"]?.yield);
  const chg = $('[data-role="ten-chg"]', card);
  chg.textContent = signedBp(tenors["10Y"]?.change_1d_bp);
  chg.className = toneClass(tenors["10Y"]?.change_1d_bp);

  // All three curves share one scale so the overlay shows real movement rather
  // than each line being independently normalised into the same band.
  const history = payload.history_curves || {};
  const scaleValues = [];
  TENORS.forEach((tenor) => {
    [
      finite(tenors?.[tenor]?.yield),
      finite(history["1m"]?.tenors?.[tenor]),
      finite(history["1y"]?.tenors?.[tenor]),
    ].forEach((value) => {
      if (value !== null) scaleValues.push(value);
    });
  });
  const scale = scaleValues.length
    ? { min: Math.min(...scaleValues), max: Math.max(...scaleValues) }
    : null;

  const points = curvePoints(tenors, scale);
  const line = $('[data-role="curve-line"]', card);
  const dots = $('[data-role="curve-dots"]', card);
  dots.innerHTML = "";
  if (points) {
    line.setAttribute("points", points.map((p) => `${p.x},${p.y}`).join(" "));
    points.forEach((point) => {
      const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dot.setAttribute("cx", point.x);
      dot.setAttribute("cy", point.y);
      dot.setAttribute("r", "3.5");
      dot.setAttribute("class", "curve-dot");
      dots.append(dot);
    });
  } else {
    line.setAttribute("points", "");
  }

  [
    ["1m", '[data-role="curve-1m"]'],
    ["1y", '[data-role="curve-1y"]'],
  ].forEach(([key, selector]) => {
    const target = $(selector, card);
    if (!target) return;
    const snapshot = history[key]?.tenors;
    const past = snapshot
      ? curvePoints(
          Object.fromEntries(TENORS.map((tenor) => [tenor, { yield: snapshot[tenor] }])),
          scale,
        )
      : null;
    target.setAttribute("points", past ? past.map((p) => `${p.x},${p.y}`).join(" ") : "");
  });

  const axis = $('[data-role="curve-axis"]', card);
  axis.innerHTML = "";
  TENORS.forEach((tenor) => {
    const cell = document.createElement("span");
    cell.textContent = `${tenor} ${pct(tenors[tenor]?.yield, 2)}`;
    axis.append(cell);
  });

  const stats = $('[data-role="stats"]', card);
  stats.innerHTML = "";
  const rows = [
    ["10Y-2Y", plainBp(structure.spread_10y_2y_bp), shapeLabel[structure.shape] || ""],
    ["30Y-10Y", plainBp(structure.spread_30y_10y_bp), ""],
    ["10Y 一月变动", signedBp(tenors["10Y"]?.change_1m_bp), ""],
    [
      "10Y 两年分位",
      finite(tenors["10Y"]?.percentile_2y) === null
        ? "—"
        : `${finite(tenors["10Y"].percentile_2y).toFixed(1)}%`,
      "",
    ],
    ["数据日期", payload.as_of || "—", ""],
  ];
  rows.forEach(([label, value, note]) => {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = note ? `${value} · ${note}` : value;
    stats.append(dt, dd);
  });
}

function renderSpreads(data) {
  const grid = $("#spread-grid");
  grid.innerHTML = "";
  Object.entries(data.cross_spreads || {}).forEach(([key, info]) => {
    const card = document.createElement("article");
    card.className = "spread-card";

    const label = document.createElement("h3");
    const percentile = finite(info.percentile_2y);
    label.textContent =
      percentile === null ? info.label || key : `${info.label || key}（${percentile.toFixed(0)} 分位）`;

    // The level itself carries no direction, so only the change chips get a
    // red/green tone - colouring the level too would read as a move.
    const value = document.createElement("strong");
    value.textContent = plainBp(info.spread_bp);

    const changes = document.createElement("div");
    changes.className = "spread-changes";
    [
      ["1日", info.change_1d_bp],
      ["1周", info.change_1w_bp],
      ["1月", info.change_1m_bp],
      ["1年", info.change_1y_bp],
    ].forEach(([name, raw]) => {
      const chip = document.createElement("span");
      chip.className = toneClass(raw);
      chip.textContent = `${name} ${signedBp(raw)}`;
      changes.append(chip);
    });

    card.append(label, value, changes);
    grid.append(card);
  });
}

function renderTable(data) {
  const body = $("#detail-body");
  body.innerHTML = "";
  MARKETS.forEach((market) => {
    const payload = data.markets?.[market];
    if (!payload) return;
    TENORS.forEach((tenor, index) => {
      const info = payload.tenors?.[tenor] || {};
      const row = document.createElement("tr");

      const marketCell = document.createElement("th");
      marketCell.scope = "row";
      marketCell.textContent = index === 0 ? payload.name_zh || market : "";
      if (index === 0) marketCell.className = "market-cell";

      const tenorCell = document.createElement("td");
      tenorCell.textContent = tenor;

      const yieldCell = document.createElement("td");
      yieldCell.className = "mono";
      yieldCell.textContent = pct(info.yield);

      row.append(marketCell, tenorCell, yieldCell);

      changeColumns.forEach(([key]) => {
        const cell = document.createElement("td");
        cell.className = `mono ${toneClass(info[key])}`;
        cell.textContent = signedBp(info[key]);
        row.append(cell);
      });

      const sigmaCell = document.createElement("td");
      sigmaCell.className = "mono muted-cell";
      sigmaCell.textContent = plainBp(info[SIGMA_COLUMN[0]]);
      row.append(sigmaCell);

      const percentileCell = document.createElement("td");
      percentileCell.className = "mono";
      const percentile = finite(info.percentile_2y);
      percentileCell.textContent = percentile === null ? "—" : `${percentile.toFixed(1)}%`;
      row.append(percentileCell);

      body.append(row);
    });
  });
}

function renderSources(data) {
  const list = $("#source-list");
  list.innerHTML = "";
  MARKETS.forEach((market) => {
    const payload = data.markets?.[market];
    if (!payload) return;
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = payload.source_url || "#";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = payload.source || payload.source_url || "";
    item.append(document.createTextNode(`${payload.name_zh || market}：`), link);
    list.append(item);
  });

  const check = data.us_crosscheck || {};
  if (check.note) {
    const item = document.createElement("li");
    item.textContent = `美债交叉校验：${check.note}`;
    list.append(item);
  }
}

const SVG_NS = "http://www.w3.org/2000/svg";
const TREND_COLORS = ["#4867a8", "#b64634", "#1f6a4d", "#b57a21"];

function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

/**
 * Draw a multi-series time chart. Every series shares one y-scale and one date
 * axis, so the lines are directly comparable; dates are positioned by actual
 * calendar time rather than by index, which keeps holidays and differing
 * observation counts from distorting the shape.
 */
function renderTrendChart(svg, legendHost, series, { unit = "%", digits = 2 } = {}) {
  svg.innerHTML = "";
  if (legendHost) legendHost.innerHTML = "";

  const usable = series.filter((entry) => (entry.points || []).length > 1);
  if (!usable.length) {
    svg.append(
      svgEl("text", { x: 360, y: 130, "text-anchor": "middle", class: "trend-empty" }),
    );
    svg.lastChild.textContent = "暂无历史数据";
    return;
  }

  const width = 720;
  const height = 260;
  const pad = { top: 16, right: 14, bottom: 26, left: 52 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const times = [];
  const values = [];
  usable.forEach((entry) => {
    entry.points.forEach(([stamp, value]) => {
      const time = Date.parse(stamp);
      const parsed = finite(value);
      if (Number.isFinite(time) && parsed !== null) {
        times.push(time);
        values.push(parsed);
      }
    });
  });
  if (!times.length) return;

  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  const spanT = maxT - minT || 1;
  let minV = Math.min(...values);
  let maxV = Math.max(...values);
  const padV = (maxV - minV || 1) * 0.08;
  minV -= padV;
  maxV += padV;
  const spanV = maxV - minV || 1;

  const xOf = (time) => pad.left + ((time - minT) / spanT) * plotW;
  const yOf = (value) => pad.top + plotH - ((value - minV) / spanV) * plotH;

  // Horizontal gridlines with value labels.
  const ticks = 4;
  for (let i = 0; i <= ticks; i += 1) {
    const value = minV + (spanV * i) / ticks;
    const y = yOf(value);
    svg.append(
      svgEl("line", { x1: pad.left, y1: y, x2: width - pad.right, y2: y, class: "trend-grid" }),
    );
    const label = svgEl("text", { x: pad.left - 8, y: y + 3.5, "text-anchor": "end", class: "trend-tick" });
    label.textContent = `${value.toFixed(digits)}${unit === "%" ? "%" : ""}`;
    svg.append(label);
  }

  // Zero line matters for spreads, which cross sign.
  if (minV < 0 && maxV > 0) {
    const y = yOf(0);
    svg.append(
      svgEl("line", { x1: pad.left, y1: y, x2: width - pad.right, y2: y, class: "trend-zero" }),
    );
  }

  usable.forEach((entry, index) => {
    const color = entry.color || TREND_COLORS[index % TREND_COLORS.length];
    const coords = entry.points
      .map(([stamp, value]) => {
        const time = Date.parse(stamp);
        const parsed = finite(value);
        if (!Number.isFinite(time) || parsed === null) return null;
        return `${xOf(time).toFixed(1)},${yOf(parsed).toFixed(1)}`;
      })
      .filter(Boolean)
      .join(" ");
    svg.append(
      svgEl("polyline", { points: coords, class: "trend-line", stroke: color }),
    );

    if (legendHost) {
      const chip = document.createElement("span");
      chip.className = "trend-legend-item";
      const swatch = document.createElement("i");
      swatch.style.background = color;
      const last = entry.points[entry.points.length - 1];
      const lastValue = finite(last?.[1]);
      chip.append(swatch);
      chip.append(
        document.createTextNode(
          lastValue === null
            ? entry.label
            : `${entry.label} ${lastValue.toFixed(digits)}${unit === "%" ? "%" : "bp"}`,
        ),
      );
      legendHost.append(chip);
    }
  });

  // Date axis: first, middle and last observation.
  [minT, minT + spanT / 2, maxT].forEach((time, index) => {
    const label = svgEl("text", {
      x: xOf(time),
      y: height - 8,
      "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle",
      class: "trend-tick",
    });
    label.textContent = new Date(time).toISOString().slice(0, 7);
    svg.append(label);
  });
}

function renderTrends(data) {
  renderTrendChart(
    $("#trend-yields"),
    $("#trend-yields-legend"),
    MARKETS.map((market) => ({
      label: data.markets?.[market]?.name_zh || market,
      points: data.markets?.[market]?.ten_year_history || [],
    })),
    { unit: "%", digits: 2 },
  );

  renderTrendChart(
    $("#trend-spreads"),
    $("#trend-spreads-legend"),
    Object.values(data.cross_spreads || {}).map((info) => ({
      label: info.label || "",
      points: info.history || [],
    })),
    { unit: "bp", digits: 0 },
  );
}

function renderError(message) {
  $("#hero-word").textContent = "读数失败";
  $("#hero-summary").textContent = message;
  $("#quality-state").textContent = "数据不可用";
  $("#quality-state").classList.add("is-degraded");
}

async function main() {
  let data;
  try {
    const response = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    data = await response.json();
  } catch (error) {
    renderError(`无法加载 ${DATA_URL}（${error.message}）。`);
    return;
  }

  renderQuality(data);
  renderHero(data);
  renderAlerts(data);
  $$(".market-card").forEach((card) => {
    const market = card.dataset.market;
    const payload = data.markets?.[market];
    if (payload) renderMarketCard(card, market, payload);
  });
  renderSpreads(data);
  renderTrends(data);
  renderTable(data);
  renderSources(data);
}

main();
