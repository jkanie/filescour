"""Command line: python -m filescour COMMAND

  init-db        create the tables (safe to run again)
  add PATH [--extract-unknown]
                 add a folder to search. --extract-unknown reads the text
                 of files whose Windows attributes cannot be seen (a Windows
                 drive seen from WSL). Online-only files are never read.
  scan [ID]      scan 1 folder, or every folder with no ID
  work           extract text for every queued file
  roots          list the folders
  fetch [--keep] PATH...
                 queue online-only files to down-load and read: by default
                 each is set back to online only afterwards, with --keep it
                 stays on this device (Windows only when run)
  fetch-run [--free-floor-mb N] [--max-file-mb N] [--wait-seconds N]
                 run the queued down-loads, and finish any left half done.
                 Defaults: 2048 MB left free, files up to 500 MB, 120 seconds
                 per file, or the FILESCOUR_FETCH_* settings in the environment
"""

from __future__ import annotations

import sys

from . import db, fetch, indexer, jobs


def _options(rest: list[str]) -> dict | None:
    """--free-floor-mb N --max-file-mb N --wait-seconds N, each optional."""
    names = {"--free-floor-mb": "free_floor_mb", "--max-file-mb": "max_file_mb",
             "--wait-seconds": "wait_seconds"}
    out: dict = {}
    if len(rest) % 2:
        return None
    for flag, value in zip(rest[::2], rest[1::2]):
        if flag not in names or not value.isdigit():
            return None
        out[names[flag]] = int(value)
    return out


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    with db.connect() as conn:
        db.init_schema(conn)
        if cmd == "init-db":
            print("schema ready")
        elif cmd == "add" and len(rest) in (1, 2) and (
                len(rest) == 1 or rest[1] == "--extract-unknown"):
            rid = indexer.add_root(conn, rest[0], extract_unknown=len(rest) == 2)
            print(f"folder id {rid}")
        elif cmd == "scan" and len(rest) <= 1:
            ids = [int(rest[0])] if rest else [
                r[0] for r in conn.execute("SELECT id FROM roots ORDER BY id")
            ]
            for root_id in ids:
                r = indexer.scan_root(conn, root_id)
                print(f"folder {root_id}: {r.added} added, {r.changed} changed, "
                      f"{r.unchanged} unchanged, {r.removed} removed, {r.queued} queued, "
                      f"{r.held} not read (online only or cloud state not known)")
        elif cmd == "work" and not rest:
            n = jobs.run(conn)
            s = jobs.summary(conn)
            print(f"{n} jobs run, {s['done']} done, {s['failed']} failed, "
                  f"{s['skipped']} skipped in total")
        elif cmd == "fetch" and [a for a in rest if a != "--keep"]:
            mode = "keep" if "--keep" in rest else "temporary"
            r = fetch.request(conn, [a for a in rest if a != "--keep"], mode)
            print(f"{r.queued} queued ({r.bytes} bytes), {r.not_online} not online only, "
                  f"{r.no_text} with no text to read")
        elif cmd == "fetch-run" and (opts := _options(rest)) is not None:
            try:
                r = fetch.run(conn, lim=fetch.limits(**opts), progress=print)
            except ValueError as exc:
                print(f"filescour: {exc}", file=sys.stderr)
                return 2
            except fetch.FetchUnavailable as exc:
                print(f"filescour: {exc}", file=sys.stderr)
                return 1
            print(f"{r.done} done, {r.skipped} skipped, {r.waiting} waiting to go back"
                  + (f", stopped: {r.stopped}" if r.stopped else ""))
        elif cmd == "roots" and not rest:
            for root_id, path in conn.execute("SELECT id, path FROM roots ORDER BY id"):
                print(f"{root_id}\t{path}")
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
