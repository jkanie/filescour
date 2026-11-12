// Turns the search form into the query string that the API reads.
// Kept free of React, such that it can be tested on its own.

export type Mode = "name" | "path" | "regex" | "words";
export type Source = "" | "file" | "pdf" | "doc" | "none";
export type Cloud = "" | "local" | "online" | "unknown";
// The API's sort keys. A leading "-" sorts high to low.
export type Sort =
  | "path"
  | "-path"
  | "name"
  | "-name"
  | "size"
  | "-size"
  | "modified"
  | "-modified";

export interface SearchForm {
  q: string;
  mode: Mode;
  ignoreCase: boolean;
  folder: string;
  ext: string; // comma-separated, dots optional: "pdf, .md"
  minSizeMb: string; // as typed, in MB
  maxSizeMb: string;
  modifiedFrom: string; // yyyy-mm-dd from a date input, or ""
  modifiedTo: string; // yyyy-mm-dd, the last day to include, or ""
  source: Source;
  cloud: Cloud;
  sort: Sort;
}

export const emptyForm: SearchForm = {
  q: "",
  mode: "name",
  ignoreCase: true,
  folder: "",
  ext: "",
  minSizeMb: "",
  maxSizeMb: "",
  modifiedFrom: "",
  modifiedTo: "",
  source: "",
  cloud: "",
  sort: "path",
};

const MB = 1024 * 1024;

export class FormError extends Error {}

export function mbToBytes(text: string, label: string): number | null {
  const t = text.trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isFinite(n) || n < 0) {
    throw new FormError(`${label} must be a number of MB, 0 or more.`);
  }
  return Math.round(n * MB);
}

function dayStartUtc(day: string, label: string): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  const d = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
  if (!d || d.getUTCDate() !== +m![3]) {
    throw new FormError(`${label} must be a date such as 2026-01-31.`);
  }
  return d;
}

export function splitExts(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(",")) {
    const e = part.trim().replace(/^\.+/, "").toLowerCase();
    if (e && !out.includes(e)) out.push(e);
  }
  return out;
}

// Dates are whole UTC days. "To" includes its whole day, so it is sent as
// the start of the next day, which the API treats as an exclusive bound.
export function toParams(form: SearchForm): URLSearchParams {
  const p = new URLSearchParams();
  p.set("q", form.q);
  p.set("mode", form.mode);
  p.set("ignore_case", String(form.ignoreCase));
  if (form.folder.trim()) p.set("folder", form.folder.trim());
  for (const e of splitExts(form.ext)) p.append("ext", e);
  const min = mbToBytes(form.minSizeMb, "Smallest size");
  const max = mbToBytes(form.maxSizeMb, "Largest size");
  if (min !== null && max !== null && min > max) {
    throw new FormError("Smallest size is larger than largest size.");
  }
  if (min !== null) p.set("min_size", String(min));
  if (max !== null) p.set("max_size", String(max));
  if (form.modifiedFrom) {
    p.set("modified_after", dayStartUtc(form.modifiedFrom, "Modified from").toISOString());
  }
  if (form.modifiedTo) {
    const end = dayStartUtc(form.modifiedTo, "Modified to");
    end.setUTCDate(end.getUTCDate() + 1);
    p.set("modified_before", end.toISOString());
  }
  if (form.source) p.set("source", form.source);
  if (form.cloud) p.set("cloud", form.cloud);
  // The API sorts by path when no sort is sent, so the default stays out of the URL.
  if (form.sort !== "path") p.set("sort", form.sort);
  if ((form.mode === "regex" || form.mode === "words") && !form.q.trim()) {
    throw new FormError(
      form.mode === "regex" ? "Type a regular expression to search for." : "Type at least 1 word.",
    );
  }
  return p;
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

// The 3 limits of a down-load run, as typed. Empty means "use the API's own
// setting", which comes from the environment or the built-in default.
export interface LimitsForm {
  freeFloorMb: string;
  maxFileMb: string;
  waitSeconds: string;
}

export const emptyLimits: LimitsForm = { freeFloorMb: "", maxFileMb: "", waitSeconds: "" };

export function limitParams(form: LimitsForm): URLSearchParams {
  const p = new URLSearchParams();
  const fields: [keyof LimitsForm, string, string][] = [
    ["freeFloorMb", "free_floor_mb", "Space to leave free"],
    ["maxFileMb", "max_file_mb", "Largest file"],
    ["waitSeconds", "wait_seconds", "Wait per file"],
  ];
  for (const [key, name, label] of fields) {
    const t = form[key].trim();
    if (t === "") continue;
    if (!/^\d+$/.test(t)) throw new FormError(`${label} must be a whole number, 0 or more.`);
    p.set(name, String(Number(t)));
  }
  return p;
}
