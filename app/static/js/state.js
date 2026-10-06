// state.js - global dashboard state + hash-URL routing.
//
// Hash scheme (every view is linkable; refresh keeps state):
//   #/                                       home (repo list + ingest)
//   #/repo/<id>                              repository overview
//   #/repo/<id>/dir/<enc path>               directory view
//   #/repo/<id>/file/<enc path>              file view
//   #/repo/<id>/authors[/<enc path>]         author leaderboard + merges
//   #/repo/<id>/commits                      commit-set panel
// Filter query params: author, mode (all|range|list), from, to, shas, b (day|week).
import { API } from "./api.js";

export function defaultFilters() {
  return { author: null, mode: "all", from: null, to: null, shas: [], bucket: "week" };
}

export const state = {
  repos: [],
  repoId: null,
  repo: null,
  authors: [],
  merges: [],
  contextRepoId: null,
  computeStatus: null,
  repoJobs: new Map(),      // repoId -> last compute status seen (home table + polling)
  prevJobStates: new Map(), // repoId -> previous job state (done/failed transitions)
  route: null,
  routeToken: 0,
  picked: new Map(),        // sha -> commit object cached from the picker (subjects)
  autoCompute: new Set(),   // repo ids to compute as soon as ingestion finishes
};

// ---------------------------------------------------------------------------
// Route <-> hash
// ---------------------------------------------------------------------------

export function parseHash(raw = location.hash) {
  const hash = String(raw || "").replace(/^#\/?/, "");
  const queryIndex = hash.indexOf("?");
  const pathPart = queryIndex === -1 ? hash : hash.slice(0, queryIndex);
  const params = new URLSearchParams(queryIndex === -1 ? "" : hash.slice(queryIndex + 1));
  const segments = pathPart.split("/").filter(Boolean).map((part) => {
    try { return decodeURIComponent(part); } catch (_error) { return part; }
  });

  const f = defaultFilters();
  const author = Number(params.get("author"));
  if (Number.isInteger(author) && author > 0) f.author = author;
  const mode = params.get("mode");
  if (mode === "range" || mode === "list") f.mode = mode;
  for (const key of ["from", "to"]) {
    const value = Number(params.get(key));
    if (Number.isInteger(value) && value > 0) f[key] = value;
  }
  const shas = (params.get("shas") || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (shas.length) f.shas = shas;
  const bucket = params.get("b");
  if (bucket === "day" || bucket === "week") f.bucket = bucket;

  if (segments[0] === "repo" && /^\d+$/.test(segments[1] || "")) {
    const repoId = Number(segments[1]);
    const kind = segments[2] || "";
    const rest = segments[3] || "";
    if (kind === "file" && rest) return { view: "file", repoId, path: rest, filters: f };
    if (kind === "authors") return { view: "authors", repoId, path: rest, filters: f };
    if (kind === "commits") return { view: "commits", repoId, path: "", filters: f };
    if (kind === "dir" && rest) return { view: "object", repoId, path: rest, filters: f };
    return { view: "object", repoId, path: "", filters: f };
  }
  return { view: "home", repoId: null, path: "", filters: defaultFilters() };
}

export function buildHash(route) {
  const { view = "object", repoId = null, path = "", filters: f = defaultFilters() } = route || {};
  if (view === "home" || !repoId) return "#/";
  let segments;
  if (view === "file") {
    segments = ["repo", String(repoId), "file", encodeURIComponent(path || "")];
  } else if (view === "authors") {
    segments = ["repo", String(repoId), "authors"];
    if (path) segments.push(encodeURIComponent(path));
  } else if (view === "commits") {
    segments = ["repo", String(repoId), "commits"];
  } else {
    segments = path ? ["repo", String(repoId), "dir", encodeURIComponent(path)] : ["repo", String(repoId)];
  }
  const params = new URLSearchParams();
  if (f.author != null) params.set("author", String(f.author));
  if (f.mode === "range") {
    params.set("mode", "range");
    if (f.from != null) params.set("from", String(f.from));
    if (f.to != null) params.set("to", String(f.to));
  } else if (f.mode === "list") {
    params.set("mode", "list");
    if (f.shas && f.shas.length) params.set("shas", f.shas.join(","));
  }
  if (f.bucket && f.bucket !== "week") params.set("b", f.bucket);
  const qs = params.toString();
  return "#/" + segments.join("/") + (qs ? "?" + qs : "");
}

export function navigate(route, { replace = false } = {}) {
  const hash = buildHash(route);
  if (location.hash === hash) {
    window.dispatchEvent(new CustomEvent("rat:refresh"));
    return;
  }
  if (replace) {
    history.replaceState(null, "", hash);
    window.dispatchEvent(new CustomEvent("rat:refresh"));
  } else {
    location.hash = hash;
  }
}

export const go = {
  home: () => navigate({ view: "home", repoId: null, path: "", filters: defaultFilters() }),
  repo: (repoId) => navigate({ view: "object", repoId, path: "", filters: defaultFilters() }),
  dir: (repoId, path, f) => navigate({ view: "object", repoId, path: path || "", filters: f || filters() }),
  file: (repoId, path, f) => navigate({ view: "file", repoId, path, filters: f || filters() }),
  authors: (repoId, path, f) => navigate({ view: "authors", repoId, path: path || "", filters: f || filters() }),
  commits: (repoId, f) => navigate({ view: "commits", repoId, path: "", filters: f || filters() }),
};

// ---------------------------------------------------------------------------
// Current filters
// ---------------------------------------------------------------------------

export function filters() {
  const f = state.route ? state.route.filters : defaultFilters();
  return { ...defaultFilters(), ...f, shas: [...(f.shas || [])] };
}

export function setFilters(partial) {
  if (!state.route) return;
  navigate({ ...state.route, filters: { ...filters(), ...partial } });
}

export function clearFilters() {
  if (!state.route) return;
  navigate({ ...state.route, filters: defaultFilters() });
}

export function commitSetBody(f) {
  const active = f || filters();
  const body = { mode: active.mode };
  if (active.mode === "range") {
    if (active.from != null) body.from = active.from;
    if (active.to != null) body.to = active.to;
  } else if (active.mode === "list") {
    body.shas = [...active.shas];
  }
  return body;
}

export function objectFor(route) {
  if (route.view === "file") return { type: "file", path: route.path };
  if (route.path) return { type: "directory", path: route.path };
  return { type: "repository" };
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

export async function loadRepos() {
  const data = await API.listRepos();
  state.repos = data.repos || [];
  return state.repos;
}

export async function loadRepoContext(repoId) {
  if (state.contextRepoId !== repoId) {
    state.repo = null;
    state.authors = [];
    state.merges = [];
    state.contextRepoId = repoId;
  }
  const [repo, authors] = await Promise.all([API.getRepo(repoId), API.listAuthors(repoId)]);
  state.repoId = repoId;
  state.repo = repo;
  state.authors = authors.authors || [];
  state.merges = authors.merges || [];
  return repo;
}

export function authorById(authorId) {
  if (authorId == null) return null;
  return state.authors.find(
    (a) => a.author_id === authorId || (a.member_ids || []).includes(authorId)
  ) || null;
}
