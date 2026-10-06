// views.js - one renderer per route. Every number rendered here comes from
// the API responses; nothing is recomputed client-side.
import { API } from "./api.js";
import * as ui from "./ui.js";
import * as charts from "./charts.js";
import * as shell from "./shell.js";
import { state, filters, go, commitSetBody, objectFor, buildHash, authorById } from "./state.js";

// ---------------------------------------------------------------------------
// Shared bits
// ---------------------------------------------------------------------------

function skeletonView(label) {
  return `<div class="sk-page">${ui.loadingBlock(label)}${ui.skeletonTable(5, 5)}</div>`;
}

function truncate(value, max) {
  const s = String(value ?? "");
  return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
}

function commitSetLabel(cs) {
  if (!cs || cs.mode === "all") return "all time (H\u0304)";
  if (cs.mode === "range") {
    const from = cs.from != null ? ui.fmtEpochDay(cs.from) : "start";
    const to = cs.to != null ? ui.fmtEpochDay(cs.to - 1) : "now";
    return `${from} \u2192 ${to}`;
  }
  const count = cs.shas_count != null ? cs.shas_count : 0;
  return `${ui.fmtInt(count)} picked commit${count === 1 ? "" : "s"}`;
}

function objectLabel(object) {
  if (object.type === "repository") return "repository root";
  if (object.type === "directory") return `directory ${object.path}/`;
  return `file ${object.path}`;
}

function chartScopeNote(data, f) {
  if (f.author != null && data.author_scope) {
    return `author filter active \u2014 showing ${data.author_scope.label}'s own series (|H| = ${ui.fmtInt(data.author_scope.h_size)})`;
  }
  if (f.author != null) return "author filter active";
  return `all authors \u00b7 |H| = ${ui.fmtInt(data.scope.h_size)}`;
}

const CARDS = [
  { key: "added", sym: "l+", label: "lines added" },
  { key: "removed", sym: "l\u2212", label: "lines removed" },
  { key: "growth", sym: "\u03b4", label: "growth (l+ \u2212 l\u2212)" },
  { key: "churn", sym: "\u03bb", label: "churn (l+ + l\u2212)" },
  { key: "modifications", sym: "n", label: "modifications" },
  { key: "frequency", sym: "\u03b7", label: "frequency (n / |H|)" },
  { key: "churn_rate", sym: "\u03c1", label: "churn rate (\u03bb / |H|)" },
];

function metricCards(metrics) {
  return `<div class="cards">` + CARDS.map((card) => {
    const isRate = card.key === "frequency" || card.key === "churn_rate";
    const value = isRate ? ui.fmtRate(metrics[card.key]) : ui.fmtInt(metrics[card.key]);
    return `<div class="card">
      <div class="card-sym">${card.sym}</div>
      <div class="card-value">${value}</div>
      <div class="card-label">${card.label}</div>
    </div>`;
  }).join("") + `</div>`;
}

function childColumns() {
  return [
    {
      key: "name", label: "Name",
      render: (r) => `<span class="cell-name">${r.type === "directory" ? "\u25b8 " : ""}${ui.esc(r.name)}${r.type === "directory" ? "/" : ""}</span>`,
      sortValue: (r) => r.name,
    },
    { key: "type", label: "Type", render: (r) => `<span class="muted">${ui.esc(r.type)}</span>`, sortValue: (r) => r.type },
    { key: "added", label: "l+", align: "right", render: (r) => ui.fmtInt(r.added), sortValue: (r) => r.added },
    { key: "removed", label: "l\u2212", align: "right", render: (r) => ui.fmtInt(r.removed), sortValue: (r) => r.removed },
    { key: "growth", label: "\u03b4", align: "right", render: (r) => ui.fmtSigned(r.growth), sortValue: (r) => r.growth },
    { key: "churn", label: "\u03bb", align: "right", render: (r) => ui.fmtInt(r.churn), sortValue: (r) => r.churn },
    { key: "modifications", label: "n", align: "right", render: (r) => ui.fmtInt(r.modifications), sortValue: (r) => r.modifications },
    { key: "frequency", label: "\u03b7", align: "right", render: (r) => ui.fmtRate(r.frequency), sortValue: (r) => r.frequency },
    { key: "churn_rate", label: "\u03c1", align: "right", render: (r) => ui.fmtRate(r.churn_rate), sortValue: (r) => r.churn_rate },
  ];
}

function authorColumns() {
  return [
    {
      key: "label", label: "Author",
      render: (r) => {
        const info = authorById(r.author_id);
        const emails = info && info.emails && info.emails.length
          ? `<div class="muted small">${ui.esc(info.emails.join(", "))}</div>` : "";
        return `<div class="cell-strong">${ui.esc(r.label)}${r.merged ? " " + ui.badge("merged", "merged") : ""}</div>${emails}`;
      },
      sortValue: (r) => r.label,
    },
    { key: "modifications", label: "Modifications", align: "right", render: (r) => ui.fmtInt(r.modifications), sortValue: (r) => r.modifications },
    { key: "churn", label: "Churn \u03bb", align: "right", render: (r) => ui.fmtInt(r.churn), sortValue: (r) => r.churn },
    { key: "ownership", label: "Ownership \u03c9", align: "right", render: (r) => ui.fmtPercent(r.ownership), sortValue: (r) => r.ownership },
  ];
}

// ---------------------------------------------------------------------------
// Home: ingest + repository list
// ---------------------------------------------------------------------------

export function renderHome() {
  const host = document.getElementById("view");
  if (!host.querySelector("#home-root")) {
    host.innerHTML = `
      <div id="home-root">
        <section class="view-head">
          <div>
            <h2>Repositories</h2>
            <div class="muted small">Ingest a repository, compute its metrics, then explore every level with filters and charts.</div>
          </div>
        </section>
        <div class="ingest-grid">
          <form id="clone-form" class="panel">
            <h3>Clone from a URL</h3>
            <div class="row gap">
              <input class="ctl" id="clone-url" type="text" autocomplete="off" spellcheck="false"
                     placeholder="https://github.com/DaveGamble/cJSON.git" required>
              <button class="btn" type="submit">Clone</button>
            </div>
            <div class="muted small">Full clone (never shallow); ingestion runs in the background.</div>
          </form>
          <form id="upload-form" class="panel">
            <h3>Upload a zip</h3>
            <div class="row gap">
              <input class="ctl" id="upload-file" type="file" accept=".zip,application/zip" required>
              <button class="btn" type="submit">Upload</button>
            </div>
            <div class="muted small">The zip must contain the repository's .git (directory or pointer file). Max 2 GiB.</div>
          </form>
        </div>
        <section class="panel">
          <h3>All repositories <span class="muted small" id="home-poll"></span></h3>
          <div class="table-wrap" id="home-table"></div>
          <div class="empty-state" id="home-empty" hidden>
            <div class="es-title">No repositories yet</div>
            <div class="es-hint">Clone a URL or upload a zip above to get started.</div>
          </div>
        </section>
      </div>`;
    wireIngestForms();
  }
  refreshHomeTable();
}

function wireIngestForms() {
  const cloneForm = document.getElementById("clone-form");
  cloneForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.getElementById("clone-url");
    const url = input.value.trim();
    if (url) submitClone(cloneForm, input, url);
  });
  const uploadForm = document.getElementById("upload-form");
  uploadForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = document.getElementById("upload-file");
    const file = input.files && input.files[0];
    if (file) submitUpload(uploadForm, input, file);
  });
}

async function submitClone(form, input, url) {
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const result = await API.clone(url);
    input.value = "";
    state.autoCompute.add(result.repo_id);
    ui.toast("info", `Clone started for repository #${result.repo_id} \u2014 metrics compute automatically when ingestion finishes.`);
    if (result.warning === "duplicate_source") {
      ui.toast("warn", `This source was ingested before (repo ${result.duplicate_repo_ids.join(", ")}). A second copy was created.`);
    }
    window.dispatchEvent(new CustomEvent("rat:ingest-changed"));
  } catch (error) {
    ui.toast("error", error.message, { retry: () => submitClone(form, input, url) });
  } finally {
    button.disabled = false;
  }
}

async function submitUpload(form, input, file) {
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const result = await API.upload(file);
    input.value = "";
    state.autoCompute.add(result.repo_id);
    ui.toast("info", `Upload accepted for repository #${result.repo_id} \u2014 extracting, then computing metrics.`);
    if (result.warning === "duplicate_source") {
      ui.toast("warn", `This source was ingested before (repo ${result.duplicate_repo_ids.join(", ")}). A second copy was created.`);
    }
    window.dispatchEvent(new CustomEvent("rat:ingest-changed"));
  } catch (error) {
    ui.toast("error", error.message, { retry: () => submitUpload(form, input, file) });
  } finally {
    button.disabled = false;
  }
}

export function refreshHomeTable() {
  const host = document.getElementById("home-table");
  if (!host) return;
  const repos = state.repos;
  host.innerHTML = `<table class="tbl">
    <thead><tr><th>ID</th><th>Name</th><th>Source</th><th>Status</th><th>Metrics</th><th>HEAD</th><th></th></tr></thead>
    <tbody>${repos.map(homeRow).join("")}</tbody>
  </table>`;
  const empty = document.getElementById("home-empty");
  if (empty) empty.hidden = repos.length > 0;
  const poll = document.getElementById("home-poll");
  if (poll) {
    const ingesting = repos.some((r) => r.status === "uploaded" || r.status === "cloning");
    const computing = [...state.repoJobs.values()].some((job) => job && job.state === "running");
    poll.textContent = ingesting || computing ? "\u00b7 processing \u2014 live updates" : "";
  }
  host.querySelectorAll("[data-open]").forEach((el) => el.addEventListener("click", (event) => {
    event.stopPropagation();
    go.repo(Number(el.dataset.open));
  }));
  host.querySelectorAll("[data-compute]").forEach((el) => el.addEventListener("click", (event) => {
    event.stopPropagation();
    shell.startCompute(Number(el.dataset.compute));
  }));
  host.querySelectorAll("tr[data-row]").forEach((tr) => tr.addEventListener("click", () => {
    const repo = repos.find((r) => r.id === Number(tr.dataset.row));
    if (repo && repo.computed_at) go.repo(repo.id);
  }));
}

function homeRow(repo) {
  const job = state.repoJobs.get(repo.id);
  let metricsCell;
  if (job && job.state === "running") {
    const pct = Number(job.percent) || 0;
    metricsCell = `<div class="progress slim"><div class="fill" style="width:${pct}%"></div></div>
      <span class="muted small">${ui.fmtInt(job.percent)}% (${ui.fmtInt(job.processed)}/${ui.fmtInt(job.total)})</span>`;
  } else if (repo.computed_at) {
    metricsCell = `<span class="muted small">computed ${ui.esc(ui.fmtDate(repo.computed_at))}</span>`;
  } else if (job && job.state === "failed") {
    metricsCell = `<span class="err-text small">failed: ${ui.esc(truncate(job.error || "unknown error", 60))}</span>`;
  } else if (repo.status === "ready") {
    metricsCell = `<span class="muted small">not computed</span>`;
  } else {
    metricsCell = `<span class="muted small">\u2014</span>`;
  }
  let action = "";
  if (repo.computed_at) action = `<button type="button" class="btn small" data-open="${repo.id}">Open</button>`;
  else if (repo.status === "ready") action = `<button type="button" class="btn small" data-compute="${repo.id}">Compute</button>`;
  const detail = repo.status_detail
    ? `<div class="muted small">${ui.esc(truncate(repo.status_detail, 90))}</div>` : "";
  return `<tr data-row="${repo.id}" class="${repo.computed_at ? "clickable" : ""}">
    <td>${repo.id}</td>
    <td>${ui.esc(repo.name)}</td>
    <td>${ui.badge(repo.source_type, repo.source_type)} <span class="mono muted" title="${ui.esc(repo.source_ref)}">${ui.esc(truncate(repo.source_ref, 46))}</span></td>
    <td>${ui.badge(repo.status, repo.status)}${detail}</td>
    <td>${metricsCell}</td>
    <td class="mono">${ui.esc(repo.head_sha ? repo.head_sha.slice(0, 8) : "\u2014")}</td>
    <td class="num">${action}</td>
  </tr>`;
}

// ---------------------------------------------------------------------------
// Landing: ingestion / compute progress before the first computation exists
// ---------------------------------------------------------------------------

export function renderLanding(route, host) {
  const repo = state.repo;
  const status = state.computeStatus || {};
  if (repo.status === "failed" && !repo.computed_at) {
    host.innerHTML = `<section class="panel">
      <h2>Ingestion failed</h2>
      <p class="muted">${ui.esc(repo.status_detail || "The repository could not be ingested.")}</p>
      <a class="btn ghost small" href="#/">\u2190 All repositories</a>
    </section>`;
    return;
  }
  if (repo.status !== "ready") {
    host.innerHTML = `<section class="panel">
      <h2>Ingesting repository\u2026</h2>
      <div class="progress indeterminate"><div class="fill"></div></div>
      <p class="muted small">Extracting/cloning <span class="mono">${ui.esc(repo.source_ref)}</span>.
      This page updates automatically; computation starts as soon as ingestion finishes.</p>
    </section>`;
    return;
  }
  if (status.state === "running") {
    const pct = Number(status.percent) || 0;
    host.innerHTML = `<section class="panel">
      <h2>Computing metrics\u2026</h2>
      <div class="progress"><div class="fill" data-compute-fill style="width:${pct}%"></div></div>
      <p class="muted small"><span data-compute-pct>${ui.fmtInt(status.percent)}% (${ui.fmtInt(status.processed)}/${ui.fmtInt(status.total)})</span>
      commits processed. The dashboard opens automatically when computing finishes.</p>
    </section>`;
    return;
  }
  if (status.state === "failed") {
    host.innerHTML = `<section class="panel">
      <h2>Computation failed</h2>
      <p class="muted">${ui.esc(status.error || "The last computation failed.")}</p>
      <button type="button" class="btn" id="landing-compute">Retry computation</button>
    </section>`;
    host.querySelector("#landing-compute").addEventListener("click", () => shell.startCompute(repo.id));
    return;
  }
  host.innerHTML = `<section class="panel">
    <h2>Ready to compute</h2>
    <p class="muted">Computing runs one streaming <span class="mono">git log --numstat -M50%</span> pass over the full history
    (merge commits excluded, binary files flagged) and stores per-commit rows. Re-running is always safe.</p>
    <button type="button" class="btn" id="landing-compute">Compute metrics</button>
  </section>`;
  host.querySelector("#landing-compute").addEventListener("click", () => shell.startCompute(repo.id));
}

// ---------------------------------------------------------------------------
// Repository / directory view (unified renderer)
// ---------------------------------------------------------------------------

export async function renderObject(route, host, token) {
  const repoId = route.repoId;
  const f = filters();
  const object = objectFor(route);
  host.innerHTML = skeletonView("Loading directory metrics\u2026");

  const body = { object, commit_set: commitSetBody(f) };
  if (f.author != null) body.author = f.author;
  const [data, kids] = await Promise.all([API.metrics(repoId, body), API.children(repoId, body)]);
  if (token !== state.routeToken) return;

  const isRoot = object.type === "repository";
  const title = isRoot
    ? `${state.repo ? state.repo.name : "repository"} \u2014 overview`
    : `${object.path.split("/").pop()}/`;
  const parentPath = isRoot ? null : object.path.split("/").slice(0, -1).join("/");
  const upLink = parentPath === null
    ? ""
    : `<a class="btn ghost small" href="${buildHash({ view: "object", repoId, path: parentPath, filters: f })}">\u2191 Up</a>`;
  const authorsLink = buildHash({
    view: "authors", repoId,
    path: object.type === "directory" ? object.path : "",
    filters: f,
  });
  const hZero = data.scope.h_size === 0;
  const emptyMessage = hZero
    ? ui.emptyState("No commits in selection", "Every metric below is zero. Widen the date range, clear filters, or pick different commits.")
    : "";

  host.innerHTML = `
    <section class="view-head">
      <div>
        <h2>${ui.esc(title)}</h2>
        <div class="muted small">${ui.esc(objectLabel(object))} \u00b7 commit set: ${ui.esc(commitSetLabel(data.commit_set))}</div>
      </div>
      <div class="view-actions">
        ${upLink}
        <a class="btn ghost small" href="${authorsLink}">Authors</a>
        <a class="btn ghost small" href="${buildHash({ view: "commits", repoId, filters: f })}">Commit set</a>
      </div>
    </section>
    ${emptyMessage}
    ${metricCards(data.object_metrics)}
    <div class="chart-grid">
      <section class="panel">
        <h3>Churn over time</h3>
        <div class="muted small">${ui.esc(chartScopeNote(data, f))} \u00b7 bucket: ${f.bucket}</div>
        <div class="chart" id="chart-churn"></div>
      </section>
      <section class="panel">
        <h3>Top modified children</h3>
        <div class="muted small">by churn (l+ + l\u2212) at this level</div>
        <div class="chart" id="chart-top"></div>
      </section>
      <section class="panel">
        <h3>Author ownership</h3>
        <div class="muted small">share of the object's churn (\u03c9)</div>
        <div class="chart" id="chart-owner"></div>
      </section>
      <section class="panel">
        <h3>Churn treemap</h3>
        <div class="muted small">current level \u2014 click a tile to drill in</div>
        <div class="chart" id="chart-tree"></div>
      </section>
    </div>
    <section class="panel">
      <h3>Children <span class="muted small">${ui.fmtInt(kids.children.length)} entries</span></h3>
      <div class="table-wrap" id="children-table"></div>
    </section>`;

  const series = f.author != null && data.author_scope ? data.author_scope.series : data.series;
  charts.mountChart(host.querySelector("#chart-churn"), charts.churnOverTimeOption(series, f.bucket));
  charts.mountChart(host.querySelector("#chart-top"), charts.topFilesOption(kids.children));
  charts.mountChart(host.querySelector("#chart-owner"), charts.ownershipOption(data.authors));
  const treeChart = charts.mountChart(host.querySelector("#chart-tree"), charts.treemapOption(kids.children));
  if (treeChart) {
    treeChart.on("click", (params) => {
      const d = params.data;
      if (!d || d.path === undefined) return;
      if (d.kind === "directory") go.dir(repoId, d.path, f);
      else go.file(repoId, d.path, f);
    });
  }

  const table = ui.sortableTable({
    columns: childColumns(),
    rows: kids.children,
    initialSort: { key: "churn", dir: "desc" },
    onRowClick: (row) => {
      if (row.type === "directory") go.dir(repoId, row.path, f);
      else go.file(repoId, row.path, f);
    },
    emptyText: "No files or directories changed in this selection.",
  });
  host.querySelector("#children-table").appendChild(table.el);
  shell.updateSummary(data);
}

// ---------------------------------------------------------------------------
// File view: metrics + per-author ownership
// ---------------------------------------------------------------------------

export async function renderFile(route, host, token) {
  const repoId = route.repoId;
  const f = filters();
  const object = { type: "file", path: route.path };
  host.innerHTML = skeletonView("Loading file metrics\u2026");

  const body = { object, commit_set: commitSetBody(f) };
  if (f.author != null) body.author = f.author;
  const data = await API.metrics(repoId, body);
  if (token !== state.routeToken) return;

  const parent = route.path.split("/").slice(0, -1).join("/");
  const hZero = data.scope.h_size === 0;
  host.innerHTML = `
    <section class="view-head">
      <div>
        <h2 class="mono">${ui.esc(route.path)}</h2>
        <div class="muted small">${ui.esc(objectLabel(object))} \u00b7 commit set: ${ui.esc(commitSetLabel(data.commit_set))}</div>
      </div>
      <div class="view-actions">
        <a class="btn ghost small" href="${buildHash({ view: "object", repoId, path: parent, filters: f })}">\u2191 Up</a>
      </div>
    </section>
    ${hZero ? ui.emptyState("No commits in selection", "Every metric below is zero. Widen the date range, clear filters, or pick different commits.") : ""}
    ${metricCards(data.object_metrics)}
    <div class="chart-grid">
      <section class="panel">
        <h3>Churn over time</h3>
        <div class="muted small">${ui.esc(chartScopeNote(data, f))} \u00b7 bucket: ${f.bucket}</div>
        <div class="chart" id="chart-churn"></div>
      </section>
      <section class="panel">
        <h3>Ownership</h3>
        <div class="muted small">share of the file's churn (\u03c9)</div>
        <div class="chart" id="chart-owner"></div>
      </section>
    </div>
    <section class="panel">
      <h3>Ownership by author <span class="muted small">${ui.fmtInt(data.authors.length)} identities</span></h3>
      <div class="table-wrap" id="owner-table"></div>
    </section>`;

  const series = f.author != null && data.author_scope ? data.author_scope.series : data.series;
  charts.mountChart(host.querySelector("#chart-churn"), charts.churnOverTimeOption(series, f.bucket));
  charts.mountChart(host.querySelector("#chart-owner"), charts.ownershipOption(data.authors));

  const table = ui.sortableTable({
    columns: authorColumns(),
    rows: data.authors,
    initialSort: { key: "churn", dir: "desc" },
    emptyText: "No authors changed this file in the selection.",
  });
  host.querySelector("#owner-table").appendChild(table.el);
  shell.updateSummary(data);
}

// ---------------------------------------------------------------------------
// Author view: leaderboard + merge panel
// ---------------------------------------------------------------------------

export async function renderAuthors(route, host, token) {
  const repoId = route.repoId;
  const f = filters();
  const object = objectFor(route);
  host.innerHTML = skeletonView("Loading authors\u2026");

  // The leaderboard always covers the full H of the commit set; the author
  // filter itself is intentionally not applied here (it is edited here).
  const data = await API.metrics(repoId, { object, commit_set: commitSetBody(f) });
  if (token !== state.routeToken) return;

  host.innerHTML = `
    <section class="view-head">
      <div>
        <h2>Authors</h2>
        <div class="muted small">leaderboard over ${ui.esc(objectLabel(object))} \u00b7 ${ui.esc(commitSetLabel(data.commit_set))}
        \u00b7 the author filter does not restrict this view</div>
      </div>
    </section>
    <div class="chart-grid">
      <section class="panel">
        <h3>Ownership (\u03c9)</h3>
        <div class="muted small">share of churn in the current selection</div>
        <div class="chart" id="chart-owner"></div>
      </section>
      <section class="panel">
        <h3>Merge identities</h3>
        <div class="muted small">merging updates the dropdown, ownership and every breakdown</div>
        <div id="merge-panel"></div>
      </section>
    </div>
    <section class="panel">
      <h3>Leaderboard <span class="muted small">${ui.fmtInt(data.authors.length)} identities</span></h3>
      <div class="table-wrap" id="board"></div>
    </section>`;

  charts.mountChart(host.querySelector("#chart-owner"), charts.ownershipOption(data.authors));
  buildMergePanel(host.querySelector("#merge-panel"));

  const table = ui.sortableTable({
    columns: authorColumns(),
    rows: data.authors,
    initialSort: { key: "churn", dir: "desc" },
    emptyText: "No authors in this selection.",
  });
  host.querySelector("#board").appendChild(table.el);
  shell.updateSummary(data);
}

function buildMergePanel(container) {
  const repoId = state.repoId;
  container.innerHTML = `
    <div class="merge-panel">
      <div class="muted small">Select two or more identities (same person, different names/emails) and merge them into one canonical author.</div>
      <input class="ctl" id="merge-search" type="text" placeholder="Filter identities\u2026" autocomplete="off" spellcheck="false">
      <div class="merge-list" id="merge-list"></div>
      <div class="merge-actions">
        <input class="ctl" id="merge-label" type="text" placeholder="Optional label" autocomplete="off">
        <button type="button" class="btn small" id="btn-merge" disabled>Merge selected</button>
      </div>
      <div class="merges" id="merges-list"></div>
    </div>`;
  const listEl = container.querySelector("#merge-list");
  const searchEl = container.querySelector("#merge-search");
  const labelEl = container.querySelector("#merge-label");
  const mergeBtn = container.querySelector("#btn-merge");
  const checkedIds = () => [...listEl.querySelectorAll('input[type="checkbox"]:checked')].map((i) => Number(i.value));

  function paintList(filterText) {
    const needle = (filterText || "").trim().toLowerCase();
    const items = state.authors.filter((a) => !needle || a.label.toLowerCase().includes(needle));
    listEl.innerHTML = items.map((a) => `
      <label class="merge-item">
        <input type="checkbox" value="${a.author_id}">
        <span class="cell-strong">${ui.esc(a.label)}</span>
        ${a.merged ? ui.badge("merged", "merged") : ""}
        <span class="muted small">${ui.fmtInt(a.commit_count)} commits</span>
      </label>`).join("") || `<div class="muted small">No matching identities.</div>`;
    mergeBtn.disabled = true;
  }

  listEl.addEventListener("change", () => { mergeBtn.disabled = checkedIds().length < 2; });
  searchEl.addEventListener("input", () => paintList(searchEl.value));
  mergeBtn.addEventListener("click", async () => {
    const ids = checkedIds();
    if (ids.length < 2) return;
    mergeBtn.disabled = true;
    try {
      await API.createMerge(repoId, ids, labelEl.value.trim() || null);
      ui.toast("success", `Merged ${ids.length} identities \u2014 every view now treats them as one author.`);
      window.dispatchEvent(new CustomEvent("rat:refresh"));
    } catch (error) {
      mergeBtn.disabled = false;
      ui.toast("error", error.message, { retry: () => mergeBtn.click() });
    }
  });
  paintList();
  paintMerges(container.querySelector("#merges-list"));
}

function paintMerges(host) {
  const merges = state.merges || [];
  if (!merges.length) {
    host.innerHTML = `<div class="fb-label">Merge groups</div><div class="muted small">No merge groups yet.</div>`;
    return;
  }
  host.innerHTML = `<div class="fb-label">Merge groups</div>` + merges.map((m) => `
    <div class="merge-group">
      <div class="merge-group-main">
        <div class="cell-strong">${ui.esc(m.label)}</div>
        <div class="muted small">${ui.esc((m.members || []).map((x) => x.name || `author ${x.author_id}`).join(", "))}
        \u00b7 ${ui.fmtInt(m.commit_count)} commits \u00b7 created ${ui.esc(ui.fmtDate(m.created_at))}</div>
      </div>
      <button type="button" class="btn ghost small" data-unmerge="${m.group_id}">Unmerge</button>
    </div>`).join("");
  host.querySelectorAll("[data-unmerge]").forEach((btn) => {
    let armed = false;
    let timer = null;
    btn.addEventListener("click", async () => {
      if (!armed) {
        armed = true;
        btn.textContent = "Confirm?";
        btn.classList.add("danger");
        timer = setTimeout(() => {
          armed = false;
          btn.textContent = "Unmerge";
          btn.classList.remove("danger");
        }, 6000);
        return;
      }
      clearTimeout(timer);
      btn.disabled = true;
      try {
        await API.unmerge(state.repoId, Number(btn.dataset.unmerge));
        ui.toast("success", "Group dissolved \u2014 every member is its own identity again.");
        window.dispatchEvent(new CustomEvent("rat:refresh"));
      } catch (error) {
        btn.disabled = false;
        ui.toast("error", error.message, { retry: () => btn.click() });
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Commit-set panel
// ---------------------------------------------------------------------------

export async function renderCommits(route, host, token) {
  const repoId = route.repoId;
  const f = filters();
  host.innerHTML = skeletonView("Loading the commit set\u2026");

  const body = { object: { type: "repository" }, commit_set: commitSetBody(f) };
  if (f.author != null) body.author = f.author;
  const data = await API.metrics(repoId, body);
  if (token !== state.routeToken) return;

  const cs = data.commit_set;
  const listMode = cs.mode === "list";
  const requested = listMode ? (cs.shas_count != null ? cs.shas_count : f.shas.length) : null;
  const hSize = data.scope.h_size;
  const series = f.author != null && data.author_scope ? data.author_scope.series : data.series;
  const rows = series.map((s) => {
    const picked = state.picked.get(s.sha);
    return { ...s, subject: picked && picked.subject ? picked.subject : "" };
  });

  let emptyMessage = "";
  if (listMode && requested > 0 && hSize === 0) {
    emptyMessage = ui.emptyState("None of the picked commits are in H\u0304",
      "They may be merge commits, or the selection is stale after a recompute. Pick different commits or clear the filter.");
  } else if (hSize === 0) {
    emptyMessage = ui.emptyState("No commits in selection",
      "Every metric below is zero. Widen the range, clear filters, or pick different commits.");
  }

  host.innerHTML = `
    <section class="view-head">
      <div>
        <h2>Commit set</h2>
        <div class="muted small">scope: ${ui.esc(commitSetLabel(cs))} \u00b7 object: repository root</div>
      </div>
      <div class="view-actions">
        <a class="btn ghost small" href="${buildHash({ view: "object", repoId, filters: f })}">Repository overview</a>
        ${listMode ? `<span class="chip accent">requested ${ui.fmtInt(requested)} \u00b7 in H\u0304 ${ui.fmtInt(hSize)}</span>` : ""}
      </div>
    </section>
    ${emptyMessage}
    ${metricCards(data.object_metrics)}
    <div class="chart-grid">
      <section class="panel">
        <h3>Churn over time</h3>
        <div class="muted small">${ui.esc(chartScopeNote(data, f))} \u00b7 bucket: ${f.bucket}</div>
        <div class="chart" id="chart-churn"></div>
      </section>
    </div>
    ${listMode && rows.length ? `
    <section class="panel">
      <h3>Selected commits <span class="muted small">${ui.fmtInt(rows.length)} with changes in H\u0304</span></h3>
      <div class="table-wrap" id="commit-table"></div>
    </section>` : ""}`;

  charts.mountChart(host.querySelector("#chart-churn"), charts.churnOverTimeOption(series, f.bucket));

  if (listMode && rows.length) {
    const table = ui.sortableTable({
      columns: [
        { key: "sha", label: "Commit", render: (r) => `<span class="mono">${ui.esc(ui.shortSha(r.sha))}</span>`, sortValue: (r) => r.sha },
        { key: "date", label: "Date", render: (r) => ui.esc(ui.fmtEpochDay(r.date)), sortValue: (r) => r.date },
        { key: "subject", label: "Subject", render: (r) => ui.esc(r.subject || "\u2014"), sortValue: (r) => r.subject },
        { key: "added", label: "l+", align: "right", render: (r) => ui.fmtInt(r.added), sortValue: (r) => r.added },
        { key: "removed", label: "l\u2212", align: "right", render: (r) => ui.fmtInt(r.removed), sortValue: (r) => r.removed },
      ],
      rows,
      initialSort: { key: "date", dir: "desc" },
      emptyText: "No rows.",
    });
    host.querySelector("#commit-table").appendChild(table.el);
  }
  shell.updateSummary(data);
}
