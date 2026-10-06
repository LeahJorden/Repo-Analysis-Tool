// Thin API client for the RAT backend. Every number the dashboard renders
// comes from these endpoints - nothing is recomputed client-side.
const BASE = "/api";

function queryString(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? "?" + qs : "";
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(BASE + path, options);
  } catch (_cause) {
    throw new Error("Network error - the server did not respond.");
  }
  let data = null;
  try {
    data = await response.json();
  } catch (_error) {
    /* non-JSON body */
  }
  if (!response.ok) {
    const detail = data && data.detail;
    const error = new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

const jsonBody = (body) => ({
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

export const API = {
  listRepos: () => request("/repos"),
  getRepo: (repoId) => request(`/repos/${repoId}`),
  clone: (url) => request("/repos/clone", jsonBody({ url })),
  upload: (file) => {
    const body = new FormData();
    body.append("file", file);
    return request("/repos/upload", { method: "POST", body });
  },
  compute: (repoId, body) =>
    request(`/repos/${repoId}/compute`, body ? jsonBody(body) : { method: "POST" }),
  computeStatus: (repoId) => request(`/repos/${repoId}/compute/status`),
  listAuthors: (repoId) => request(`/repos/${repoId}/authors`),
  createMerge: (repoId, aliases, label) =>
    request(`/repos/${repoId}/authors/merge`, jsonBody({ aliases, label })),
  unmerge: (repoId, groupId) =>
    request(`/repos/${repoId}/authors/merges/${groupId}`, { method: "DELETE" }),
  listCommits: (repoId, params) => request(`/repos/${repoId}/commits${queryString(params)}`),
  tree: (repoId, path) => request(`/repos/${repoId}/tree${queryString({ path })}`),
  metrics: (repoId, body) => request(`/repos/${repoId}/metrics/query`, jsonBody(body)),
  children: (repoId, body) => request(`/repos/${repoId}/metrics/children`, jsonBody(body)),
};
