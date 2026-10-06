// ui.js - DOM + formatting helpers shared by every dashboard view.

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function h(html) {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}

// ---------------------------------------------------------------------------
// Formatting (display only - numbers themselves always come from the API)
// ---------------------------------------------------------------------------

export function fmtInt(n) {
  const value = Number(n);
  if (!Number.isFinite(value)) return "0";
  return value.toLocaleString("en-US");
}

export function fmtSigned(n) {
  const value = Number(n) || 0;
  return (value > 0 ? "+" : "") + fmtInt(value);
}

export function fmtRate(x) {
  const value = Number(x);
  if (!value) return "0";
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 1 ? 2 : 4;
  return value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

export function fmtPercent(x) {
  const value = Number(x) || 0;
  if (!value) return "0%";
  return (value * 100).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "%";
}

export function fmtDate(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString("en-US", {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

export const DAY_SECONDS = 86400;

export function fmtEpochDay(sec) {
  const date = new Date(Number(sec) * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
}

export function dayValueToEpoch(value) {
  const [y, m, d] = String(value).split("-").map(Number);
  return Math.floor(Date.UTC(y, m - 1, d) / 1000);
}

export function shortSha(sha, len = 10) {
  return String(sha || "").slice(0, len);
}

export function badge(kind, text) {
  return `<span class="badge ${esc(kind)}">${esc(text)}</span>`;
}

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------

export function toast(kind, message, opts = {}) {
  const host = document.getElementById("toasts");
  if (!host) return null;
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<span class="toast-msg">${esc(message)}</span>`;
  const action = opts.retry ? { label: "Retry", run: opts.retry } : opts.action;
  if (action) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn ghost small";
    btn.textContent = action.label;
    btn.addEventListener("click", () => { el.remove(); action.run(); });
    el.appendChild(btn);
  }
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "\u00d7";
  close.addEventListener("click", () => el.remove());
  el.appendChild(close);
  host.appendChild(el);
  const ttl = opts.ttl ?? (kind === "error" ? 12000 : 6000);
  if (ttl > 0) setTimeout(() => el.remove(), ttl);
  return el;
}

// ---------------------------------------------------------------------------
// Loading / empty states
// ---------------------------------------------------------------------------

export function loadingBlock(label = "Loading\u2026") {
  return `<div class="loading-block"><span class="spinner"></span>${esc(label)}</div>`;
}

export function skeletonTable(rows = 5, cols = 6) {
  const cells = Array.from({ length: cols }, () => `<div class="sk"></div>`).join("");
  return `<div class="sk-table">${Array.from({ length: rows }, () => `<div class="sk-row">${cells}</div>`).join("")}</div>`;
}

export function emptyState(title, hint = "") {
  return `<div class="empty-state"><div class="es-title">${esc(title)}</div>` +
    (hint ? `<div class="es-hint">${esc(hint)}</div>` : "") + `</div>`;
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------

export function openModal({ title = "", size = "md" } = {}) {
  const root = document.getElementById("modal-root");
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.innerHTML = `
    <div class="modal ${esc(size)}" role="dialog" aria-modal="true">
      <div class="modal-head">
        <h3>${esc(title)}</h3>
        <button type="button" class="toast-close" aria-label="Close">\u00d7</button>
      </div>
      <div class="modal-body"></div>
      <div class="modal-foot"></div>
    </div>`;
  root.appendChild(backdrop);
  const close = () => {
    backdrop.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (event) => { if (event.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  backdrop.addEventListener("click", (event) => { if (event.target === backdrop) close(); });
  backdrop.querySelector(".modal-head .toast-close").addEventListener("click", close);
  return {
    root: backdrop,
    body: backdrop.querySelector(".modal-body"),
    foot: backdrop.querySelector(".modal-foot"),
    close,
  };
}

// ---------------------------------------------------------------------------
// Sortable table
// ---------------------------------------------------------------------------

// columns: [{ key, label, align: "right"?, render(row)->html, sortValue(row),
//             sortable: true|false }]
export function sortableTable({ columns, rows = [], initialSort = null, onRowClick = null, emptyText = "Nothing to show." }) {
  let sortKey = initialSort ? initialSort.key : null;
  let sortDir = initialSort ? (initialSort.dir || "desc") : "desc";
  let data = rows;
  let painted = [];

  const table = document.createElement("table");
  table.className = "tbl";
  const thead = document.createElement("thead");
  const tbody = document.createElement("tbody");
  table.append(thead, tbody);

  const raw = (row, col) => (col.sortValue ? col.sortValue(row) : row[col.key]);
  const display = (row, col) => (col.render ? col.render(row) : esc(row[col.key] ?? "\u2014"));

  function sorted() {
    if (!sortKey) return data;
    const col = columns.find((c) => c.key === sortKey);
    if (!col) return data;
    const dir = sortDir === "asc" ? 1 : -1;
    return [...data].sort((a, b) => {
      const va = raw(a, col);
      const vb = raw(b, col);
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
      return String(va ?? "").localeCompare(String(vb ?? "")) * dir;
    });
  }

  function paint() {
    thead.innerHTML = "<tr>" + columns.map((col) => {
      const isSorted = col.key === sortKey;
      const arrow = isSorted ? (sortDir === "asc" ? " \u25b2" : " \u25bc") : "";
      const classes = [
        col.align === "right" ? "num" : "",
        col.sortable === false ? "" : "sortable",
        isSorted ? "sorted" : "",
      ].filter(Boolean).join(" ");
      return `<th class="${classes}" data-key="${esc(col.key)}" title="Sort by ${esc(col.label)}">${esc(col.label)}${arrow}</th>`;
    }).join("") + "</tr>";

    painted = sorted();
    if (!painted.length) {
      tbody.innerHTML = `<tr><td class="empty-cell" colspan="${columns.length}">${esc(emptyText)}</td></tr>`;
      return;
    }
    tbody.innerHTML = painted.map((row, index) => {
      const cells = columns.map((col) => {
        const classes = [col.align === "right" ? "num" : "", col.className || ""].filter(Boolean).join(" ");
        return `<td class="${classes}">${display(row, col)}</td>`;
      }).join("");
      return `<tr data-index="${index}"${onRowClick ? ' class="clickable"' : ""}>${cells}</tr>`;
    }).join("");
  }

  thead.addEventListener("click", (event) => {
    const th = event.target.closest("th");
    if (!th) return;
    const col = columns.find((c) => c.key === th.dataset.key);
    if (!col || col.sortable === false) return;
    if (sortKey === col.key) {
      sortDir = sortDir === "asc" ? "desc" : "asc";
    } else {
      sortKey = col.key;
      sortDir = col.align === "right" ? "desc" : "asc";
    }
    paint();
  });

  if (onRowClick) {
    tbody.addEventListener("click", (event) => {
      const tr = event.target.closest("tr[data-index]");
      if (!tr) return;
      const row = painted[Number(tr.dataset.index)];
      if (row) onRowClick(row, event);
    });
  }

  paint();
  return {
    el: table,
    render(nextRows) { data = nextRows || []; paint(); },
    setSort(key, dir = "desc") { sortKey = key; sortDir = dir; paint(); },
  };
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

export function debounce(fn, ms = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_error) { /* fall through to the legacy path */ }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch (_error) {
    return false;
  }
}
