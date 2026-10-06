// shell.js - persistent chrome around every repository view: topbar (repo
// switcher + compute controls), filter bar (author / commit set / bucket),
// lazy file tree, breadcrumbs, summary chips and the commit picker modal.
import { API } from "./api.js";
import * as ui from "./ui.js";
import { state, filters, setFilters, clearFilters, go, buildHash, objectFor, authorById } from "./state.js";

const MAX_PICK = 200;

// ---------------------------------------------------------------------------
// Shell visibility
// ---------------------------------------------------------------------------

export function initShell() {
  const sidebar = document.getElementById("sidebar");
  sidebar.addEventListener("click", onSidebarClick);
}

export function prepareRepoShell() {
  document.body.classList.remove("at-home");
  document.getElementById("sidebar").hidden = false;
  document.getElementById("filterbar").hidden = false;
  document.getElementById("crumbs").hidden = false;
}

export function showHomeShell() {
  document.body.classList.add("at-home");
  document.getElementById("sidebar").hidden = true;
  document.getElementById("filterbar").hidden = true;
  document.getElementById("crumbs").hidden = true;
}

// ---------------------------------------------------------------------------
// Topbar: repo switcher + compute area
// ---------------------------------------------------------------------------

export function renderTopbar(route) {
  const host = document.getElementById("topbar");
  const options = [`<option value="">All repositories</option>`].concat(
    state.repos.map((r) =>
      `<option value="${r.id}" ${r.id === route.repoId ? "selected" : ""}>#${r.id} ${ui.esc(r.name)}</option>`)
  ).join("");
  host.innerHTML = `
    <button type="button" class="brand" id="brand-btn" title="All repositories">
      <b>RAT</b><span>Repo Analysis Tool</span>
    </button>
    <div class="topbar-repo">
      <span class="fb-label">Repository</span>
      <select class="ctl" id="repo-switcher">${options}</select>
    </div>
    <div class="compute-area" id="compute-area"></div>`;
  host.querySelector("#brand-btn").addEventListener("click", () => go.home());
  host.querySelector("#repo-switcher").addEventListener("change", (event) => {
    if (event.target.value) go.repo(Number(event.target.value));
    else go.home();
  });
  paintComputeArea();
}

function paintComputeArea() {
  const host = document.getElementById("compute-area");
  if (!host) return;
  const repo = state.repo;
  if (!repo) { host.innerHTML = ""; return; }
  const status = state.computeStatus || {};
  if (status.state === "running") {
    const pct = Number(status.percent) || 0;
    host.innerHTML = `
      <div class="compute-live">
        <div class="progress slim"><div class="fill" data-compute-fill style="width:${pct}%"></div></div>
        <span class="muted small" data-compute-pct>${ui.fmtInt(status.percent)}% (${ui.fmtInt(status.processed)}/${ui.fmtInt(status.total)})</span>
      </div>`;
    return;
  }
  const computed = repo.computed_at ? `computed ${ui.fmtDate(repo.computed_at)}` : "not computed yet";
  host.innerHTML = `
    <input id="ref-input" class="ctl ref-input" type="text" spellcheck="false"
           placeholder="reference sha (optional)" value="${ui.esc(repo.reference_commit ? repo.reference_commit.slice(0, 10) : "")}">
    <button type="button" class="btn small" id="btn-recompute">${repo.computed_at ? "Recompute" : "Compute"}</button>
    <span class="muted small">${ui.esc(computed)}</span>`;
  const run = () => {
    const input = host.querySelector("#ref-input");
    startCompute(repo.id, input ? input.value.trim() : "");
  };
  host.querySelector("#btn-recompute").addEventListener("click", run);
  host.querySelector("#ref-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") run();
  });
}

export async function startCompute(repoId, referenceCommit) {
  const ref = (referenceCommit || "").trim();
  try {
    await API.compute(repoId, ref ? { reference_commit: ref } : undefined);
    ui.toast("info", ref
      ? `Reference commit set to ${ref.slice(0, 10)} \u2014 recomputing\u2026`
      : "Computation started\u2026");
    state.computeStatus = { state: "running", percent: 0, processed: 0, total: 0 };
    window.dispatchEvent(new CustomEvent("rat:compute-started", { detail: { repoId } }));
  } catch (error) {
    ui.toast("error", error.message, { retry: () => startCompute(repoId, ref) });
  }
}

export function setComputeStatus(status) {
  state.computeStatus = status;
  paintComputeArea();
  const pct = status && status.state === "running"
    ? (Number(status.percent) || 0)
    : (status && status.state === "done" ? 100 : 0);
  for (const fill of document.querySelectorAll("[data-compute-fill]")) {
    fill.style.width = pct + "%";
  }
  for (const label of document.querySelectorAll("[data-compute-pct]")) {
    label.textContent = status && status.state === "running"
      ? `${ui.fmtInt(status.percent)}% (${ui.fmtInt(status.processed)}/${ui.fmtInt(status.total)})`
      : (status && status.state === "done" ? "100%" : "");
  }
}

// ---------------------------------------------------------------------------
// Filter bar
// ---------------------------------------------------------------------------

export function renderFilterBar() {
  const host = document.getElementById("filterbar");
  host.hidden = false;
  const f = filters();
  const authorOptions = [`<option value="">All authors</option>`].concat(
    state.authors.map((a) =>
      `<option value="${a.author_id}" ${a.author_id === f.author ? "selected" : ""}>` +
      `${ui.esc(a.label)}${a.merged ? " \u2299 merged" : ""} \u2014 ${ui.fmtInt(a.commit_count)} commits</option>`)
  ).join("");
  host.innerHTML = `
    <div class="fb">
      <div class="fb-item">
        <span class="fb-label">Author</span>
        <select class="ctl" id="f-author">${authorOptions}</select>
      </div>
      <div class="fb-item">
        <span class="fb-label">Commit set</span>
        <div class="segmented" id="cs-mode">
          <button type="button" data-mode="all" class="${f.mode === "all" ? "active" : ""}">All time</button>
          <button type="button" data-mode="range" class="${f.mode === "range" ? "active" : ""}">Date range</button>
          <button type="button" data-mode="list" class="${f.mode === "list" ? "active" : ""}">Pick commits</button>
        </div>
      </div>
      <div class="fb-item fb-detail" id="cs-detail"></div>
      <div class="fb-item">
        <span class="fb-label">Bucket</span>
        <select class="ctl" id="f-bucket">
          <option value="week" ${f.bucket === "week" ? "selected" : ""}>Week</option>
          <option value="day" ${f.bucket === "day" ? "selected" : ""}>Day</option>
        </select>
      </div>
      <div class="fb-item fb-actions">
        <button type="button" class="btn ghost small" id="btn-clear">Clear filters</button>
        <button type="button" class="btn ghost small" id="btn-copy">Copy link</button>
      </div>
    </div>
    <div class="summary-chips" id="summary-chips"></div>`;

  host.querySelector("#f-author").addEventListener("change", (event) => {
    setFilters({ author: event.target.value ? Number(event.target.value) : null });
  });
  host.querySelector("#f-bucket").addEventListener("change", (event) => {
    setFilters({ bucket: event.target.value });
  });
  host.querySelectorAll("#cs-mode button").forEach((btn) => btn.addEventListener("click", () => {
    const mode = btn.dataset.mode;
    if (mode === "list") { openPickerModal(); return; }
    if (mode !== f.mode) setFilters({ mode });
  }));
  host.querySelector("#btn-clear").addEventListener("click", () => clearFilters());
  host.querySelector("#btn-copy").addEventListener("click", async () => {
    const ok = await ui.copyText(location.href);
    ui.toast(ok ? "success" : "warn",
      ok ? "Link copied to the clipboard." : "Copy failed \u2014 copy the address bar URL instead.");
  });
  paintCommitSetDetail();
  updateSummary(null);
}

function paintCommitSetDetail() {
  const host = document.getElementById("cs-detail");
  if (!host) return;
  const f = filters();
  if (f.mode === "range") {
    host.innerHTML = `
      <span class="fb-label">Range (UTC days, inclusive)</span>
      <div class="row gap">
        <input type="date" class="ctl" id="cs-from" value="${f.from != null ? ui.fmtEpochDay(f.from) : ""}">
        <span class="muted">\u2192</span>
        <input type="date" class="ctl" id="cs-to" value="${f.to != null ? ui.fmtEpochDay(f.to - 1) : ""}">
      </div>`;
    const fromInput = host.querySelector("#cs-from");
    const toInput = host.querySelector("#cs-to");
    fromInput.addEventListener("change", () => {
      setFilters({ mode: "range", from: fromInput.value ? ui.dayValueToEpoch(fromInput.value) : null });
    });
    toInput.addEventListener("change", () => {
      setFilters({ mode: "range", to: toInput.value ? ui.dayValueToEpoch(toInput.value) + ui.DAY_SECONDS : null });
    });
  } else if (f.mode === "list") {
    host.innerHTML = `
      <span class="fb-label">Picked commits</span>
      <div class="row gap">
        <span class="chip">${ui.fmtInt(f.shas.length)} selected</span>
        <button type="button" class="btn ghost small" id="cs-edit">Edit\u2026</button>
        <button type="button" class="btn ghost small" id="cs-clear-list" title="Clear the selection (empty commit set = zeros)">\u00d7</button>
      </div>`;
    host.querySelector("#cs-edit").addEventListener("click", () => openPickerModal());
    host.querySelector("#cs-clear-list").addEventListener("click", () => setFilters({ mode: "list", shas: [] }));
  } else {
    host.innerHTML = `<span class="fb-label">All time</span><span class="muted small">every commit in H\u0304</span>`;
  }
}

export function updateSummary(data) {
  const host = document.getElementById("summary-chips");
  if (!host) return;
  const repo = state.repo;
  const chips = [];
  if (repo) chips.push(`<span class="chip">#${repo.id} ${ui.esc(repo.name)}</span>`);
  const object = data ? data.object : (state.route ? objectFor(state.route) : null);
  if (object) {
    const label = object.type === "repository" ? "repository root"
      : object.type === "directory" ? `dir ${object.path}/` : `file ${object.path}`;
    chips.push(`<span class="chip">${ui.esc(label)}</span>`);
  }
  if (data) {
    chips.push(`<span class="chip accent">|H| = ${ui.fmtInt(data.scope.h_size)}</span>`);
    if (data.scope.author_label) {
      const scope = data.author_scope ? ` \u00b7 |H| = ${ui.fmtInt(data.author_scope.h_size)}` : "";
      chips.push(`<span class="chip warn">author: ${ui.esc(data.scope.author_label)}${scope}</span>`);
    }
  } else {
    const f = filters();
    if (f.author != null) {
      const author = authorById(f.author);
      chips.push(`<span class="chip warn">author: ${ui.esc(author ? author.label : `#${f.author}`)}</span>`);
    }
    if (f.mode === "range" && (f.from != null || f.to != null)) {
      const from = f.from != null ? ui.fmtEpochDay(f.from) : "start";
      const to = f.to != null ? ui.fmtEpochDay(f.to - 1) : "now";
      chips.push(`<span class="chip">range ${ui.esc(from)} \u2192 ${ui.esc(to)}</span>`);
    } else if (f.mode === "list") {
      chips.push(`<span class="chip">${ui.fmtInt(f.shas.length)} picked</span>`);
    }
  }
  host.innerHTML = chips.join("");
}

// ---------------------------------------------------------------------------
// Breadcrumbs
// ---------------------------------------------------------------------------

export function renderBreadcrumbs(route) {
  const host = document.getElementById("crumbs");
  const repo = state.repo;
  if (!repo) { host.hidden = true; return; }
  host.hidden = false;
  const f = filters();
  const parts = [`<a href="#/">All repositories</a>`,
    `<a href="${buildHash({ view: "object", repoId: repo.id, path: "", filters: f })}">${ui.esc(repo.name)}</a>`];
  const segments = (route.path || "").split("/").filter(Boolean);
  if (route.view === "object" || route.view === "file") {
    segments.forEach((segment, index) => {
      const path = segments.slice(0, index + 1).join("/");
      if (index === segments.length - 1) {
        parts.push(`<span class="crumb-current">${ui.esc(segment)}${route.view === "object" ? "/" : ""}</span>`);
      } else {
        parts.push(`<a href="${buildHash({ view: "object", repoId: repo.id, path, filters: f })}">${ui.esc(segment)}</a>`);
      }
    });
  } else if (route.view === "authors") {
    segments.forEach((segment, index) => {
      const path = segments.slice(0, index + 1).join("/");
      parts.push(`<a href="${buildHash({ view: "object", repoId: repo.id, path, filters: f })}">${ui.esc(segment)}</a>`);
    });
    parts.push(`<span class="crumb-current">Authors</span>`);
  } else if (route.view === "commits") {
    parts.push(`<span class="crumb-current">Commit set</span>`);
  }
  host.innerHTML = parts.join(`<span class="crumb-sep">/</span>`);
}

// ---------------------------------------------------------------------------
// File tree (lazy; cached per repo id)
// ---------------------------------------------------------------------------

const treeState = { repoId: null, levels: new Map(), cap: 300 };

export async function renderSidebar(route) {
  const host = document.getElementById("sidebar");
  if (treeState.repoId !== route.repoId) {
    treeState.repoId = route.repoId;
    treeState.levels.clear();
  }
  const repo = state.repo;
  host.innerHTML = `
    <div class="tree-head">Files</div>
    <div class="tree">
      <div class="tree-row root" data-path="" data-type="root">
        <span class="tree-caret spacer"></span>
        <span class="tree-name">${ui.esc(repo ? repo.name : "repository")} /</span>
      </div>
      <div class="tree-children" id="tree-root" hidden></div>
    </div>`;
  const rootHost = host.querySelector("#tree-root");
  rootHost.hidden = false;
  try {
    await mountLevel("", rootHost);
    let prefix = "";
    for (const segment of (route.path || "").split("/").filter(Boolean)) {
      prefix = prefix ? `${prefix}/${segment}` : segment;
      await expandPath(prefix, host);
    }
  } catch (error) {
    rootHost.innerHTML = `<div class="tree-empty muted small">failed: ${ui.esc(error.message)}</div>`;
  }
  highlight(route);
}

async function mountLevel(path, container) {
  let payload = treeState.levels.get(path);
  if (!payload) {
    payload = await API.tree(treeState.repoId, path);
    treeState.levels.set(path, payload);
  }
  renderLevel(container, payload);
  container.dataset.loaded = "1";
}

function renderLevel(container, payload) {
  const dirs = payload.dirs || [];
  const files = payload.files || [];
  const showAll = container.dataset.showAll === "1";
  const dirList = showAll ? dirs : dirs.slice(0, treeState.cap);
  const fileList = showAll ? files : files.slice(0, Math.max(0, treeState.cap - dirList.length));
  const hidden = dirs.length + files.length - dirList.length - fileList.length;
  const html =
    dirList.map((d) => `
      <div class="tree-node">
        <div class="tree-row" data-path="${ui.esc(d.path)}" data-type="dir">
          <span class="tree-caret">\u25b8</span>
          <span class="tree-name">${ui.esc(d.name)}/</span>
        </div>
        <div class="tree-children" hidden></div>
      </div>`).join("") +
    fileList.map((f) => `
      <div class="tree-row" data-path="${ui.esc(f.path)}" data-type="file">
        <span class="tree-caret spacer"></span>
        <span class="tree-name">${ui.esc(f.name)}</span>
      </div>`).join("") +
    (hidden > 0 ? `<button type="button" class="btn ghost small tree-more">${ui.fmtInt(hidden)} more\u2026</button>` : "");
  container.innerHTML = html || `<div class="tree-empty muted small">${payload.exists === false ? "not present in H\u0304" : "empty"}</div>`;
}

function highlight(route) {
  const host = document.getElementById("sidebar");
  if (!host || !route) return;
  for (const row of host.querySelectorAll(".tree-row")) {
    const type = row.dataset.type;
    const path = row.dataset.path || "";
    let active = false;
    if (type === "root") active = route.path === "" && route.view === "object";
    else if (type === "dir") active = path === route.path && (route.view === "object" || route.view === "authors");
    else if (type === "file") active = path === route.path && route.view === "file";
    row.classList.toggle("active", active);
  }
  const activeRow = host.querySelector(".tree-row.active");
  if (activeRow) activeRow.scrollIntoView({ block: "nearest" });
}

async function toggleDir(row, forceOpen) {
  const node = row.parentElement;
  const children = node ? node.querySelector(".tree-children") : null;
  if (!children) return;
  const isOpen = !children.hidden;
  if (isOpen && !forceOpen) {
    children.hidden = true;
    row.classList.remove("open");
    return;
  }
  if (children.hidden && !children.dataset.loaded) {
    children.hidden = false;
    children.innerHTML = `<div class="tree-loading"><span class="spinner"></span></div>`;
    try {
      await mountLevel(row.dataset.path || "", children);
    } catch (error) {
      children.innerHTML = `<div class="tree-empty muted small">failed: ${ui.esc(error.message)}</div>`;
      return;
    }
  }
  children.hidden = false;
  row.classList.add("open");
  highlight(state.route);
}

async function expandPath(path, host) {
  const row = [...host.querySelectorAll('.tree-row[data-type="dir"]')].find((r) => r.dataset.path === path);
  if (row) await toggleDir(row, true);
}

function onSidebarClick(event) {
  const more = event.target.closest(".tree-more");
  if (more) {
    const container = more.parentElement;
    container.dataset.showAll = "1";
    const node = container.closest(".tree-node");
    const path = node ? node.querySelector(":scope > .tree-row").dataset.path : "";
    const payload = treeState.levels.get(path);
    if (payload) renderLevel(container, payload);
    return;
  }
  const row = event.target.closest(".tree-row");
  if (!row) return;
  const path = row.dataset.path || "";
  const type = row.dataset.type;
  if (type === "root") { go.dir(state.repoId, "", filters()); return; }
  if (type === "file") { go.file(state.repoId, path, filters()); return; }
  if (type === "dir") {
    if (event.target.closest(".tree-caret")) {
      toggleDir(row, false);
      return;
    }
    toggleDir(row, true);
    go.dir(state.repoId, path, filters());
  }
}

// ---------------------------------------------------------------------------
// Commit picker modal (searchable multi-select; honours the author filter)
// ---------------------------------------------------------------------------

export function openPickerModal() {
  const repoId = state.repoId;
  const f = filters();
  const selected = new Map();
  for (const sha of f.shas) selected.set(sha, state.picked.get(sha) || { sha });

  const modal = ui.openModal({ title: "Pick commits", size: "lg" });
  modal.body.innerHTML = `
    <div class="picker">
      <div class="picker-toolbar">
        <input class="ctl" id="picker-q" type="text" placeholder="Search subject or sha\u2026" autocomplete="off" spellcheck="false">
        <span class="muted small" id="picker-total"></span>
      </div>
      <div class="picker-list" id="picker-list"></div>
    </div>`;
  modal.foot.innerHTML = `
    <span class="picker-count muted" id="picker-count"></span>
    <div class="row gap">
      <button type="button" class="btn ghost" id="picker-cancel">Cancel</button>
      <button type="button" class="btn" id="picker-apply">Apply selection</button>
    </div>`;

  const qInput = modal.body.querySelector("#picker-q");
  const listEl = modal.body.querySelector("#picker-list");
  const totalEl = modal.body.querySelector("#picker-total");
  const countEl = modal.foot.querySelector("#picker-count");
  const rowData = new Map();
  let page = 1;
  let loading = false;

  function updateCount() {
    countEl.textContent = `${ui.fmtInt(selected.size)} selected (max ${MAX_PICK})`;
  }

  function appendCommits(commits) {
    const html = commits.map((c) => {
      rowData.set(c.sha, c);
      const checked = selected.has(c.sha);
      return `<label class="picker-row ${checked ? "checked" : ""}" data-sha="${ui.esc(c.sha)}">
        <input type="checkbox" ${checked ? "checked" : ""}>
        <span class="mono">${ui.esc(c.short_sha)}</span>
        <span class="picker-date muted">${ui.esc(ui.fmtEpochDay(c.committer_date))}</span>
        <span class="picker-subject">${ui.esc(c.subject || "(no subject)")}</span>
        <span class="picker-author muted small">${ui.esc(c.author_label)}</span>
      </label>`;
    }).join("");
    listEl.insertAdjacentHTML("beforeend", html);
  }

  async function loadPage(reset) {
    if (loading) return;
    loading = true;
    if (reset) {
      page = 1;
      rowData.clear();
      listEl.innerHTML = ui.loadingBlock("Loading commits\u2026");
    }
    try {
      const params = { page, per_page: 100 };
      const q = qInput.value.trim();
      if (q) params.q = q;
      if (f.author != null) params.author = f.author;
      if (f.mode === "range") {
        if (f.from != null) params.from = f.from;
        if (f.to != null) params.to = f.to;
      }
      const data = await API.listCommits(repoId, params);
      if (reset) listEl.innerHTML = "";
      if (!data.commits.length) {
        listEl.innerHTML = ui.emptyState("No commits match", "Adjust the search or the filters.");
      } else {
        appendCommits(data.commits);
        if (page * data.per_page < data.total) {
          listEl.insertAdjacentHTML("beforeend",
            `<button type="button" class="btn ghost small picker-more" id="picker-more">Load more</button>`);
        }
      }
      totalEl.textContent = `${ui.fmtInt(data.total)} commit${data.total === 1 ? "" : "s"} in H\u0304`;
    } catch (error) {
      ui.toast("error", error.message, { retry: () => loadPage(true) });
    } finally {
      loading = false;
    }
  }

  listEl.addEventListener("change", (event) => {
    const row = event.target.closest(".picker-row");
    if (!row || !event.target.matches('input[type="checkbox"]')) return;
    const sha = row.dataset.sha;
    if (event.target.checked) {
      if (selected.size >= MAX_PICK) {
        event.target.checked = false;
        ui.toast("warn", `At most ${MAX_PICK} commits can be selected.`);
        return;
      }
      selected.set(sha, rowData.get(sha) || { sha });
      row.classList.add("checked");
    } else {
      selected.delete(sha);
      row.classList.remove("checked");
    }
    updateCount();
  });

  listEl.addEventListener("click", (event) => {
    const more = event.target.closest("#picker-more");
    if (more) {
      more.remove();
      page += 1;
      loadPage(false);
    }
  });

  qInput.addEventListener("input", ui.debounce(() => loadPage(true), 300));
  modal.foot.querySelector("#picker-cancel").addEventListener("click", () => modal.close());
  modal.foot.querySelector("#picker-apply").addEventListener("click", () => {
    state.picked = new Map([...selected.entries()].map(([sha, c]) => [sha, rowData.get(sha) || c]));
    modal.close();
    setFilters({ mode: "list", shas: [...selected.keys()] });
  });

  updateCount();
  loadPage(true);
  qInput.focus();
}
