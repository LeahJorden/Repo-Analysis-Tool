// charts.js - ECharts option builders (ECharts is vendored locally; loaded as a
// plain script so `window.echarts` is available - never a CDN at runtime).
import { fmtInt } from "./ui.js";

const PALETTE = ["#4f8cff", "#2ecc71", "#f5a623", "#e05c8d", "#9b6cff", "#39d0d8",
  "#ff7a59", "#8fd14f", "#ffd166", "#7a8cff", "#52b788", "#c77dff"];

const AXIS_TEXT = "#8b93a7";
const AXIS_LINE = "#2a2f3a";
const SPLIT_LINE = "#20242e";
const TOOLTIP = {
  backgroundColor: "#171a21",
  borderColor: "#2a2f3a",
  textStyle: { color: "#e6e9ef", fontSize: 12 },
};

const registry = new Map(); // container element -> chart instance

export function mountChart(el, option) {
  if (!el || !option || !window.echarts) return null;
  let chart = window.echarts.getInstanceByDom(el);
  if (!chart) chart = window.echarts.init(el, null, { renderer: "canvas" });
  chart.setOption(option, true);
  registry.set(el, chart);
  return chart;
}

export function disposeAll() {
  for (const chart of registry.values()) {
    try { chart.dispose(); } catch (_error) { /* already gone */ }
  }
  registry.clear();
}

window.addEventListener("resize", () => {
  for (const chart of registry.values()) {
    try { chart.resize(); } catch (_error) { /* ignore */ }
  }
});

function emptyOption(text) {
  return {
    graphic: [{
      type: "text", left: "center", top: "middle",
      style: { text, fill: AXIS_TEXT, fontSize: 13 },
    }],
  };
}

// ---------------------------------------------------------------------------
// Time bucketing (UTC; weeks start on Monday)
// ---------------------------------------------------------------------------

function dayKeyOf(sec) {
  const date = new Date(Number(sec) * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
}

function weekKeyOf(sec) {
  const date = new Date(Number(sec) * 1000);
  const diff = (date.getUTCDay() + 6) % 7; // Monday = start of week
  date.setUTCDate(date.getUTCDate() - diff);
  return dayKeyOf(date.getTime() / 1000);
}

export function bucketSeries(series, bucket = "week") {
  const map = new Map();
  for (const row of series || []) {
    const key = bucket === "day" ? dayKeyOf(row.date) : weekKeyOf(row.date);
    let b = map.get(key);
    if (!b) {
      b = { key, added: 0, removed: 0, commits: 0, churn: 0 };
      map.set(key, b);
    }
    b.added += row.added;
    b.removed += row.removed;
    b.commits += 1;
  }
  const buckets = [...map.values()].sort((a, b) => (a.key < b.key ? -1 : 1));
  for (const b of buckets) b.churn = b.added + b.removed;
  return buckets;
}

// ---------------------------------------------------------------------------
// Churn over time: l+ up, l- down, churn line - bucketed day/week
// ---------------------------------------------------------------------------

export function churnOverTimeOption(series, bucket = "week") {
  const buckets = bucketSeries(series, bucket);
  if (!buckets.length) return emptyOption("No commits in this selection");
  return {
    grid: { left: 64, right: 30, top: 34, bottom: 46, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) => {
        if (!items.length) return "";
        const b = buckets[items[0].dataIndex];
        return `<b>${b.key}</b><br>commits: ${fmtInt(b.commits)}<br>` +
          `l+: ${fmtInt(b.added)}<br>l\u2212: ${fmtInt(b.removed)}<br>` +
          `churn: ${fmtInt(b.churn)}`;
      },
    },
    legend: { top: 0, textStyle: { color: AXIS_TEXT }, data: ["l+", "l\u2212", "churn"] },
    xAxis: {
      type: "category",
      data: buckets.map((b) => b.key),
      axisLabel: { color: AXIS_TEXT, hideOverlap: true },
      axisLine: { lineStyle: { color: AXIS_LINE } },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: AXIS_TEXT, formatter: (v) => fmtInt(Math.abs(v)) },
      splitLine: { lineStyle: { color: SPLIT_LINE } },
    },
    series: [
      { name: "l+", type: "bar", stack: "churn", barMaxWidth: 16, itemStyle: { color: "#2ecc71" }, data: buckets.map((b) => b.added) },
      { name: "l\u2212", type: "bar", stack: "churn", barMaxWidth: 16, itemStyle: { color: "#ff5c5c" }, data: buckets.map((b) => -b.removed) },
      {
        name: "churn", type: "line", smooth: false, symbol: "none",
        lineStyle: { color: "#4f8cff", width: 2 },
        itemStyle: { color: "#4f8cff" },
        data: buckets.map((b) => b.churn),
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// Top-N modified/churned children of the current level
// ---------------------------------------------------------------------------

export function topFilesOption(children, limit = 12) {
  const rows = (children || []).filter((c) => c.churn > 0)
    .sort((a, b) => a.churn - b.churn)
    .slice(-limit);
  if (!rows.length) return emptyOption("No churn at this level");
  return {
    grid: { left: 8, right: 30, top: 34, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) => {
        const row = rows[items[0].dataIndex];
        if (!row) return "";
        return `<b>${row.path}${row.type === "directory" ? "/" : ""}</b><br>` +
          `l+: ${fmtInt(row.added)}<br>l\u2212: ${fmtInt(row.removed)}<br>` +
          `churn: ${fmtInt(row.churn)}`;
      },
    },
    legend: { top: 0, textStyle: { color: AXIS_TEXT }, data: ["l+", "l\u2212"] },
    xAxis: {
      type: "value",
      axisLabel: { color: AXIS_TEXT, formatter: (v) => fmtInt(v) },
      splitLine: { lineStyle: { color: SPLIT_LINE } },
    },
    yAxis: {
      type: "category",
      data: rows.map((r) => r.name + (r.type === "directory" ? "/" : "")),
      axisLabel: { color: AXIS_TEXT, width: 150, overflow: "truncate" },
      axisLine: { lineStyle: { color: AXIS_LINE } },
    },
    series: [
      { name: "l+", type: "bar", stack: "churn", itemStyle: { color: "#2ecc71" }, data: rows.map((r) => r.added) },
      { name: "l\u2212", type: "bar", stack: "churn", itemStyle: { color: "#ff5c5c" }, data: rows.map((r) => r.removed) },
    ],
  };
}

// ---------------------------------------------------------------------------
// Author ownership (share of the object's churn, omega)
// ---------------------------------------------------------------------------

export function ownershipOption(authors) {
  const rows = (authors || []).filter((a) => a.churn > 0).sort((a, b) => b.churn - a.churn);
  if (!rows.length) return emptyOption("No churn in this selection");
  const top = rows.slice(0, 12);
  const rest = rows.slice(12);
  const data = top.map((a, i) => ({
    name: a.label + (a.merged ? " (merged)" : ""),
    value: a.churn,
    pct: a.ownership,
    itemStyle: { color: PALETTE[i % PALETTE.length] },
  }));
  if (rest.length) {
    data.push({
      name: `Other (${rest.length})`,
      value: rest.reduce((sum, a) => sum + a.churn, 0),
      pct: rest.reduce((sum, a) => sum + a.ownership, 0),
      itemStyle: { color: "#3a4152" },
    });
  }
  return {
    tooltip: {
      ...TOOLTIP,
      formatter: (p) => `<b>${p.name}</b><br>churn: ${fmtInt(p.value)}<br>` +
        `ownership: ${((Number(p.data.pct) || 0) * 100).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`,
    },
    legend: { type: "scroll", bottom: 0, textStyle: { color: AXIS_TEXT }, pageTextStyle: { color: AXIS_TEXT } },
    series: [{
      type: "pie",
      radius: ["34%", "60%"],
      center: ["50%", "42%"],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: "#171a21", borderWidth: 1 },
      label: {
        color: "#c9cfdb",
        formatter: (p) => (Number(p.data.pct) ? `${(p.data.pct * 100).toFixed(1)}%` : ""),
      },
      labelLine: { lineStyle: { color: AXIS_LINE } },
      data,
    }],
  };
}

// ---------------------------------------------------------------------------
// Directory treemap (churn heatmap of the current level) - the "inspired"
// visualisation. Tile area = churn, colour = churn intensity; clicking a
// directory drills in.
// ---------------------------------------------------------------------------

export function treemapOption(children) {
  const rows = (children || []).filter((c) => c.churn > 0);
  if (!rows.length) return emptyOption("No churn at this level");
  const max = Math.max(...rows.map((c) => c.churn));
  return {
    tooltip: {
      ...TOOLTIP,
      formatter: (info) => {
        const d = info.data;
        if (!d || d.value == null) return info.name;
        return `<b>${d.path}${d.kind === "directory" ? "/" : ""}</b><br>` +
          `churn: ${fmtInt(d.value)}<br>l+: ${fmtInt(d.added)}<br>l\u2212: ${fmtInt(d.removed)}<br>` +
          `<span style="color:${AXIS_TEXT}">click to open</span>`;
      },
    },
    visualMap: {
      show: false,
      min: 1,
      max: Math.max(max, 2),
      inRange: { color: ["#233350", "#2d4f8f", "#4f8cff", "#a7c8ff"] },
    },
    series: [{
      type: "treemap",
      roam: false,
      nodeClick: false,
      breadcrumb: { show: false },
      animationDuration: 300,
      itemStyle: { borderColor: "#0f1115", borderWidth: 2, gapWidth: 2 },
      label: { color: "#eaf0ff", fontSize: 11, overflow: "truncate" },
      data: rows.map((c) => ({
        name: c.name + (c.type === "directory" ? "/" : ""),
        value: c.churn,
        path: c.path,
        kind: c.type,
        added: c.added,
        removed: c.removed,
        itemStyle: c.type === "directory" ? { borderColor: "#4f8cff" } : {},
      })),
    }],
  };
}
