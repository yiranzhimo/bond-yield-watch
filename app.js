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
  const ok = quality.status === "ok";
  state.textContent = ok ? "数据正常" : "部分数据源降级";
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

function curvePoints(tenors) {
  const values = TENORS.map((tenor) => finite(tenors?.[tenor]?.yield));
  if (values.some((value) => value === null)) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
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

  const points = curvePoints(tenors);
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
  renderTable(data);
  renderSources(data);
}

main();
