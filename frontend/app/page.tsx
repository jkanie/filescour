"use client";

import {
  type Dispatch,
  FormEvent,
  type SetStateAction,
  useCallback,
  useEffect,
  useState,
} from "react";

import { api, type FetchCounts, type Hit, type JobCounts, type Root } from "@/lib/api";
import { baseName, downloadable, zipEntry } from "@/lib/hits";
import {
  type Cloud,
  emptyForm,
  emptyLimits,
  limitParams,
  type LimitsForm,
  formatSize,
  type Mode,
  pageLabel,
  pages,
  type SearchForm,
  type Sort,
  type Source,
  toParams,
} from "@/lib/query";

const MODES: { value: Mode; label: string }[] = [
  { value: "name", label: "File name" },
  { value: "path", label: "Full path" },
  { value: "regex", label: "Text (regular expression)" },
  { value: "words", label: "Text (words)" },
];

const SOURCES: { value: Source; label: string }[] = [
  { value: "", label: "Any" },
  { value: "file", label: "Text files" },
  { value: "pdf", label: "PDF text" },
  { value: "doc", label: "Word and OpenDocument text" },
  { value: "none", label: "No text indexed" },
];

const SORTS: { value: Sort; label: string }[] = [
  { value: "path", label: "Path, A to Z" },
  { value: "-path", label: "Path, Z to A" },
  { value: "name", label: "Name, A to Z" },
  { value: "-name", label: "Name, Z to A" },
  { value: "-size", label: "Largest 1st" },
  { value: "size", label: "Smallest 1st" },
  { value: "-modified", label: "Newest 1st" },
  { value: "modified", label: "Oldest 1st" },
];

const CLOUDS: { value: Cloud; label: string }[] = [
  { value: "", label: "Any" },
  { value: "local", label: "On this disk" },
  { value: "online", label: "Online only" },
  { value: "unknown", label: "Not known" },
];

const CLOUD_LABEL: Record<Hit["cloud"], string> = {
  local: "on this disk",
  online: "online only",
  unknown: "not known",
};

function Folders({
  onChange,
  limits,
  setLimits,
}: {
  onChange: () => void;
  limits: LimitsForm;
  setLimits: Dispatch<SetStateAction<LimitsForm>>;
}) {
  const [roots, setRoots] = useState<Root[]>([]);
  const [jobs, setJobs] = useState<JobCounts | null>(null);
  const [fetches, setFetches] = useState<FetchCounts | null>(null);
  const [path, setPath] = useState("");
  const [extractUnknown, setExtractUnknown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setRoots(await api.roots());
      setJobs(await api.jobs());
      const f = await api.fetches();
      setFetches(f);
      // Fill the fields once, from the API's settings. Never over what was typed.
      setLimits((cur) =>
        cur.freeFloorMb || cur.maxFileMb || cur.waitSeconds
          ? cur
          : {
              freeFloorMb: String(f.limits.free_floor_mb),
              maxFileMb: String(f.limits.max_file_mb),
              waitSeconds: String(f.limits.wait_seconds),
            },
      );
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function act(work: () => Promise<string>) {
    setBusy(true);
    setError("");
    try {
      setNote(await work());
      await refresh();
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" aria-labelledby="folders-h">
      <h2 id="folders-h">Folders</h2>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          if (path.trim()) {
            void act(async () => {
              const { id } = await api.addRoot(path.trim(), extractUnknown);
              setPath("");
              setExtractUnknown(false);
              return `Added folder ${id}. Scan it to index its files.`;
            });
          }
        }}
      >
        <label>
          Folder to add
          <input
            id="root-path"
            className="wide"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/home/jon/Documents"
          />
        </label>
        <label className="check" title="Online-only files are never read, whatever this says.">
          <input
            id="extract-unknown"
            type="checkbox"
            checked={extractUnknown}
            onChange={(e) => setExtractUnknown(e.target.checked)}
          />
          Read text when the cloud state is not known (a Windows drive seen from WSL)
        </label>
        <button type="submit" disabled={busy}>
          Add folder
        </button>
      </form>
      {roots.length === 0 ? (
        <p className="muted">No folder yet. Add one, then scan it.</p>
      ) : (
        <ul>
          {roots.map((r) => (
            <li key={r.id}>
              <span className="path">{r.path}</span> ({files(r.files)}
              {r.online > 0 && `, ${r.online} online only`}
              {r.unknown > 0 && `, ${r.unknown} cloud state not known`}
              {r.zip_entries > 0 && `, ${r.zip_entries} inside zip files`}){" "}
              <button
                className="quiet"
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    const s = await api.scan(r.id);
                    return `Scanned: ${s.added} added, ${s.changed} changed, ${s.removed} removed, ${s.queued} queued to read (text files and zip files), ${s.held} not read (online only or cloud state not known).`;
                  })
                }
              >
                Scan
              </button>{" "}
              <button
                className="quiet"
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    await api.removeRoot(r.id);
                    return "Folder removed from the index. Its files are untouched.";
                  })
                }
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      {jobs && (
        <div className="row">
          <span className="muted">
            Text extraction: {jobs.queued} queued, {jobs.done} done, {jobs.failed} failed,{" "}
            {jobs.skipped} skipped (went online only).
          </span>
          <button
            className="quiet"
            disabled={busy || jobs.queued === 0}
            onClick={() =>
              void act(async () => {
                const r = await api.runJobs();
                return `Extracted text from ${files(r.processed)}.`;
              })
            }
          >
            Extract queued text
          </button>
        </div>
      )}
      {fetches && (
        <div className="row">
          <span className="muted">
            Down-loads of online-only files: {fetches.queued} queued (
            {formatSize(fetches.queued_bytes)}), {fetches.downloading + fetches.restoring} not yet
            back in place, {fetches.done} done, {fetches.skipped} skipped.
          </span>
          <button
            className="quiet"
            disabled={busy || fetches.queued + fetches.downloading + fetches.restoring === 0}
            onClick={() =>
              void act(async () => {
                const r = await api.runFetches(limitParams(limits));
                return runNote(r);
              })
            }
          >
            Run queued down-loads
          </button>
        </div>
      )}
      <div className="row">
        <label>
          Space to leave free (MB)
          <input
            id="free-floor"
            className="narrow"
            inputMode="numeric"
            value={limits.freeFloorMb}
            onChange={(e) => setLimits({ ...limits, freeFloorMb: e.target.value })}
          />
        </label>
        <label>
          Largest file (MB)
          <input
            id="max-file"
            className="narrow"
            inputMode="numeric"
            value={limits.maxFileMb}
            onChange={(e) => setLimits({ ...limits, maxFileMb: e.target.value })}
          />
        </label>
        <label>
          Wait per file (seconds)
          <input
            id="wait"
            className="narrow"
            inputMode="numeric"
            value={limits.waitSeconds}
            onChange={(e) => setLimits({ ...limits, waitSeconds: e.target.value })}
          />
        </label>
      </div>
      {note && <p className="muted">{note}</p>}
      {error && <p className="error">{error}</p>}
    </section>
  );
}

// Export CSV and Export JSON start at the 1st match whatever page is on the
// screen, as /export gives up to 1,000 rows, not 1 page.
function fromStart(params: URLSearchParams) {
  const p = new URLSearchParams(params);
  p.delete("offset");
  return p;
}

function files(n: number) {
  return `${n} ${n === 1 ? "file" : "files"}`;
}

function runNote(r: { done: number; skipped: number; waiting: number; stopped: string | null }) {
  return (
    `Down-loads: ${r.done} done, ${r.skipped} skipped, ${r.waiting} not yet back in place.` +
    (r.stopped ? ` Stopped: ${r.stopped}.` : "")
  );
}

function OnlineHits({
  hits,
  onDone,
  limits,
}: {
  hits: Hit[];
  onDone: () => void;
  limits: LimitsForm;
}) {
  const online = downloadable(hits);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  if (online.length === 0) return null;
  const bytes = online.reduce((n, h) => n + h.size, 0);

  async function go(mode: "temporary" | "keep", now: boolean) {
    setBusy(true);
    setError("");
    try {
      const q = await api.queueFetch(
        online.map((h) => h.path),
        mode,
      );
      let text = `${q.queued} queued (${formatSize(q.bytes)}).`;
      if (now && q.queued > 0) {
        text +=
          " " +
          runNote(await api.runFetches(limitParams(limits))) +
          " Search again to see their text.";
      }
      setNote(text);
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="online">
      <p>
        {online.length} of these files {online.length === 1 ? "is" : "are"} online only (
        {formatSize(bytes)}). Their text is not searchable until they are read.
      </p>
      <div className="row">
        <button disabled={busy} onClick={() => void go("temporary", true)}>
          Read now, then put back online
        </button>
        <button className="quiet" disabled={busy} onClick={() => void go("keep", true)}>
          Read now and keep on this device
        </button>
        <button className="quiet" disabled={busy} onClick={() => void go("temporary", false)}>
          Queue for later
        </button>
      </div>
      {note && <p className="muted">{note}</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

function Results({
  hits,
  total,
  offset,
  onPage,
}: {
  hits: Hit[];
  total: number;
  offset: number;
  onPage: (offset: number) => void;
}) {
  if (hits.length === 0) {
    return <p className="muted">{offset > 0 ? `No file past number ${offset}.` : "No file matches."}</p>;
  }
  const { previous, next } = pages(offset, hits.length, total);
  return (
    <>
      <div className="row">
        <p className="muted">{pageLabel(offset, hits.length, total)}</p>
        {previous !== null && (
          <button type="button" onClick={() => onPage(previous)}>
            Previous page
          </button>
        )}
        {next !== null && (
          <button type="button" onClick={() => onPage(next)}>
            Next page
          </button>
        )}
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>File</th>
              <th className="num">Size</th>
              <th className="num">Modified</th>
              <th>Text from</th>
              <th>Data</th>
            </tr>
          </thead>
          <tbody>
            {hits.map((h) => {
              const inZip = zipEntry(h);
              return (
                <tr key={h.path}>
                  <td>
                    <div>{h.name}</div>
                    <div className="path">{h.path}</div>
                    {inZip && <div className="muted">Inside {baseName(inZip.zip)}</div>}
                    {h.lines.length > 0 && (
                      <ul className="lines">
                        {h.lines.map((ln, i) => (
                          <li key={i}>
                            {ln.line !== null && <b>{ln.line}</b>}
                            {ln.text}
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td className="num">{formatSize(h.size)}</td>
                  <td className="num">{new Date(h.modified).toLocaleString()}</td>
                  <td>
                    {h.source ?? "none"}
                    {h.truncated ? " (cut)" : ""}
                  </td>
                  <td>{CLOUD_LABEL[h.cloud]}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

export default function Page() {
  const [form, setForm] = useState<SearchForm>(emptyForm);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [params, setParams] = useState<URLSearchParams | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [foldersKey, setFoldersKey] = useState(0);
  const [limits, setLimits] = useState<LimitsForm>(emptyLimits);

  const set = <K extends keyof SearchForm>(key: K, value: SearchForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  // A new search starts at the 1st page. A page button keeps the search on the
  // screen (not any edits to the form since) and moves its start.
  async function run(e?: FormEvent, page?: number) {
    e?.preventDefault();
    setError("");
    const at = page ?? 0;
    let p: URLSearchParams;
    if (page !== undefined && params) {
      p = new URLSearchParams(params);
      p.delete("offset");
    } else {
      try {
        p = toParams(form);
      } catch (err) {
        setError((err as Error).message);
        return;
      }
    }
    if (at > 0) p.set("offset", String(at));
    setBusy(true);
    try {
      const res = await api.search(p);
      setHits(res.hits);
      setTotal(res.search.total);
      setOffset(at);
      setParams(p);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <h1>filescour</h1>
      <Folders
        key={foldersKey}
        onChange={() => setHits(null)}
        limits={limits}
        setLimits={setLimits}
      />
      <section className="panel" aria-labelledby="search-h">
        <h2 id="search-h">Search</h2>
        <form onSubmit={run} className="row">
          <label>
            Search in
            <select
              id="mode"
              value={form.mode}
              onChange={(e) => set("mode", e.target.value as Mode)}
            >
              {MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Find
            <input
              id="q"
              className="wide"
              value={form.q}
              onChange={(e) => set("q", e.target.value)}
            />
          </label>
          <label className="check">
            <input
              id="ignore-case"
              type="checkbox"
              checked={form.ignoreCase}
              onChange={(e) => set("ignoreCase", e.target.checked)}
            />
            Ignore case
          </label>
          <label>
            Inside folder
            <input
              id="folder"
              className="wide"
              value={form.folder}
              onChange={(e) => set("folder", e.target.value)}
            />
          </label>
          <label>
            File types
            <input
              id="ext"
              className="narrow"
              value={form.ext}
              placeholder="pdf, md"
              onChange={(e) => set("ext", e.target.value)}
            />
          </label>
          <label>
            Smallest (MB)
            <input
              id="min-size"
              className="narrow"
              inputMode="decimal"
              value={form.minSizeMb}
              onChange={(e) => set("minSizeMb", e.target.value)}
            />
          </label>
          <label>
            Largest (MB)
            <input
              id="max-size"
              className="narrow"
              inputMode="decimal"
              value={form.maxSizeMb}
              onChange={(e) => set("maxSizeMb", e.target.value)}
            />
          </label>
          <label>
            Modified from (UTC)
            <input
              id="from"
              type="date"
              value={form.modifiedFrom}
              onChange={(e) => set("modifiedFrom", e.target.value)}
            />
          </label>
          <label>
            Modified to (UTC)
            <input
              id="to"
              type="date"
              value={form.modifiedTo}
              onChange={(e) => set("modifiedTo", e.target.value)}
            />
          </label>
          <label>
            Text from
            <select
              id="source"
              value={form.source}
              onChange={(e) => set("source", e.target.value as Source)}
            >
              {SOURCES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Data
            <select
              id="cloud"
              value={form.cloud}
              onChange={(e) => set("cloud", e.target.value as Cloud)}
            >
              {CLOUDS.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Sort
            <select id="sort" value={form.sort} onChange={(e) => set("sort", e.target.value as Sort)}>
              {SORTS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy}>
            {busy ? "Searching" : "Search"}
          </button>
          <button type="button" className="quiet" onClick={() => setForm(emptyForm)}>
            Clear
          </button>
        </form>
        {error && <p className="error">{error}</p>}
      </section>
      {hits && (
        <section className="panel" aria-labelledby="results-h">
          <div className="row">
            <h2 id="results-h">Results</h2>
            {params && hits.length > 0 && (
              <>
                <a className="button" href={api.exportUrl(fromStart(params), "csv")}>
                  Export CSV
                </a>
                <a className="button" href={api.exportUrl(fromStart(params), "json")}>
                  Export JSON
                </a>
              </>
            )}
          </div>
          <OnlineHits
            key={params?.toString()}
            hits={hits}
            onDone={() => setFoldersKey((k) => k + 1)}
            limits={limits}
          />
          <Results hits={hits} total={total} offset={offset} onPage={(at) => void run(undefined, at)} />
        </section>
      )}
    </main>
  );
}
