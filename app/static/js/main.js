// main.js - boot, hash routing, compute/ingestion polling and refresh events.
import { API } from "./api.js";
import * as ui from "./ui.js";
import * as charts from "./charts.js";
import * as shell from "./shell.js";
import * as views from "./views.js";
import { state, parseHash, loadRepos, loadRepoContext } from "./state.js";

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

async function router() {
  const token = ++state.routeToken;
  const route = parseHash();
  state.route = route;
  charts.disposeAll();

  if (route.view === "home") {
    state.repoId = null;
    state.repo = null;
    shell.showHomeShell();
    try {
      await loadRepos();
    } catch (error) {
      ui.toast("error", "Cannot reach the server: " + error.message, { retry: runRouter });
    }
    if (token !== state.routeToken) return;
    shell.renderTopbar(route);
    views.renderHome();
    ensureTicker();
    return;
  }

  shell.prepareRepoShell();
  try {
    await loadRepoContext(route.repoId);
  } catch (error) {
    if (error.status === 404) {
      ui.toast("error", "Repository not found \u2014 back to the list.");
      location.hash = "#/";
      return;
    }
    ui.toast("error", error.message, { retry: runRouter });
    return;
  }
  if (token !== state.routeToken) return;

  shell.renderTopbar(route);
  shell.renderFilterBar();
  shell.renderBreadcrumbs(route);
  shell.renderSidebar(route).catch(() => { /* the sidebar shows its own error */ });

  let status = null;
  try {
    status = await API.computeStatus(route.repoId);
  } catch (_error) { /* the view below still renders */ }
  if (token !== state.routeToken) return;
  if (status) shell.setComputeStatus(status);

  const host = document.getElementById("view");
  const repo = state.repo;
  const computed = Boolean(repo.computed_at);
  if (repo.status === "failed" && !computed) {
    views.renderLanding(route, host);
    ensureTicker();
    return;
  }
  if (!computed) {
    views.renderLanding(route, host);
    ensureTicker();
    return;
  }

  try {
    if (route.view === "file") await views.renderFile(route, host, token);
    else if (route.view === "authors") await views.renderAuthors(route, host, token);
    else if (route.view === "commits") await views.renderCommits(route, host, token);
    else await views.renderObject(route, host, token);
  } catch (error) {
    if (token !== state.routeToken) return;
    ui.toast("error", error.message, { retry: runRouter });
    host.innerHTML = ui.emptyState("Could not load this view", error.message);
  }
  ensureTicker();
}

function runRouter() {
  router().catch((error) => {
    ui.toast("error", "Unexpected error: " + error.message, { retry: runRouter });
  });
}

// ---------------------------------------------------------------------------
// Polling: ingestion status + compute progress
// ---------------------------------------------------------------------------

let tickTimer = null;
const TICK_MS = 1200;

function needsTicker() {
  if (state.autoCompute.size) return true;
  if (state.repos.some((r) => r.status === "uploaded" || r.status === "cloning")) return true;
  for (const job of state.repoJobs.values()) {
    if (job && job.state === "running") return true;
  }
  return false;
}

function startTicker() {
  if (tickTimer) return;
  tickTimer = setInterval(tick, TICK_MS);
  tick().catch(() => { /* tick is best-effort */ });
}

function ensureTicker() {
  if (tickTimer || !needsTicker()) return;
  startTicker();
}

async function tick() {
  try {
    await loadRepos();
  } catch (_error) { /* keep the previous list */ }

  // Auto-compute any repo that just finished ingesting from this session.
  for (const repo of state.repos) {
    if (!state.autoCompute.has(repo.id)) continue;
    if (repo.status === "ready") {
      state.autoCompute.delete(repo.id);
      ui.toast("info", `Repository #${repo.id} ingested \u2014 computing metrics\u2026`);
      shell.startCompute(repo.id);
    } else if (repo.status === "failed") {
      state.autoCompute.delete(repo.id);
    }
  }

  // Watch compute jobs: the current repo + anything ready-but-never-computed.
  const watch = new Set();
  if (state.repoId) watch.add(state.repoId);
  for (const repo of state.repos) {
    if (repo.status === "ready" && !repo.computed_at) watch.add(repo.id);
  }
  for (const id of watch) {
    let status = null;
    try {
      status = await API.computeStatus(id);
    } catch (_error) {
      continue;
    }
    const prev = state.prevJobStates.get(id);
    state.repoJobs.set(id, status);
    if (id === state.repoId) shell.setComputeStatus(status);
    if (prev === "running" && status.state === "done") {
      ui.toast("success", `Metrics computed for repository #${id}.`);
      if (id === state.repoId) window.dispatchEvent(new CustomEvent("rat:refresh"));
    } else if (prev === "running" && status.state === "failed") {
      ui.toast("error", `Computation failed for #${id}: ${status.error || "unknown error"}`,
        { retry: () => shell.startCompute(id) });
      if (id === state.repoId) window.dispatchEvent(new CustomEvent("rat:refresh"));
    }
    state.prevJobStates.set(id, status.state);
  }

  if (state.route && state.route.view === "home") views.refreshHomeTable();
  if (!needsTicker()) {
    clearInterval(tickTimer);
    tickTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Events + boot
// ---------------------------------------------------------------------------

window.addEventListener("hashchange", runRouter);
window.addEventListener("rat:refresh", runRouter);
window.addEventListener("rat:ingest-changed", () => {
  startTicker();
  views.refreshHomeTable();
});
window.addEventListener("rat:compute-started", (event) => {
  const id = event.detail && event.detail.repoId;
  if (id) state.prevJobStates.set(id, "running");
  startTicker();
  if (state.route && state.route.view === "home") views.refreshHomeTable();
  if (state.route && state.route.repoId === id && !(state.repo && state.repo.computed_at)) {
    views.renderLanding(state.route, document.getElementById("view"));
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) ensureTicker();
});

async function boot() {
  shell.initShell();
  try {
    await loadRepos();
  } catch (error) {
    ui.toast("error", "Cannot reach the server: " + error.message, { retry: boot });
  }
  await router();
  ensureTicker();
}

boot().catch((error) => {
  ui.toast("error", "Failed to start the dashboard: " + error.message, { retry: boot });
});
