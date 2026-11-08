// Thin client for the filescour API.

export const API_BASE = (process.env.NEXT_PUBLIC_FILESCOUR_API ?? "http://localhost:8000").replace(
  /\/+$/,
  "",
);

export interface Line {
  line: number | null;
  text: string;
}

export interface Hit {
  path: string;
  name: string;
  ext: string;
  size: number;
  modified: string;
  source: string | null;
  cloud: "local" | "online" | "unknown";
  truncated: boolean;
  lines: Line[];
}

export interface SearchResponse {
  search: { total: number; returned: number };
  hits: Hit[];
}

export interface Root {
  id: number;
  path: string;
  extract_unknown: boolean;
  files: number;
  online: number;
  unknown: number;
}

export interface JobCounts {
  queued: number;
  running: number;
  done: number;
  failed: number;
  skipped: number;
}

export interface FetchCounts {
  queued: number;
  downloading: number;
  restoring: number;
  done: number;
  skipped: number;
  queued_bytes: number;
  limits: { free_floor_mb: number; max_file_mb: number; wait_seconds: number };
}

export interface FetchQueued {
  queued: number;
  bytes: number;
  not_online: number;
  no_text: number;
}

export interface FetchRun {
  done: number;
  skipped: number;
  waiting: number;
  stopped: string | null;
  totals: FetchCounts;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, init);
  } catch {
    throw new Error(`The API at ${API_BASE} did not answer. Is it running?`);
  }
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body?.detail;
    const text =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d: { msg?: string }) => d.msg).join(" ")
          : `HTTP ${res.status}`;
    throw new Error(text);
  }
  return body as T;
}

export const api = {
  search: (params: URLSearchParams) => call<SearchResponse>(`/search?${params}`),
  exportUrl: (params: URLSearchParams, format: "csv" | "json") =>
    `${API_BASE}/export?format=${format}&${params}`,
  roots: () => call<Root[]>("/roots"),
  addRoot: (path: string, extractUnknown: boolean) =>
    call<{ id: number }>("/roots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, extract_unknown: extractUnknown }),
    }),
  removeRoot: (id: number) => call<void>(`/roots/${id}`, { method: "DELETE" }),
  scan: (id: number) =>
    call<{
      added: number;
      changed: number;
      removed: number;
      queued: number;
      held: number;
    }>(`/roots/${id}/scan`, { method: "POST" }),
  jobs: () => call<JobCounts>("/jobs"),
  fetches: () => call<FetchCounts>("/fetch"),
  queueFetch: (paths: string[], mode: "temporary" | "keep") =>
    call<FetchQueued>("/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, mode }),
    }),
  runFetches: (limits: URLSearchParams) =>
    call<FetchRun>(`/fetch/run?limit=1000&${limits}`, { method: "POST" }),
  runJobs: () =>
    call<JobCounts & { processed: number }>("/jobs/run?limit=1000", {
      method: "POST",
    }),
};
