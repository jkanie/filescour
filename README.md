# filescour

A web app that indexes the folders you choose and searches them by file name, by full path, by the text inside the files (regular expressions or words). Every search takes the same filters, and the results export to CSV or JSON.

An example: search the text for `4417` and get every file, in all the folders you added, whose text holds `4417`, each with its matching lines. For OneDrive or Dropbox folders, it never down-loads a file behind your back. On request, it down-loads online-only files, reads them, then puts each one back online only.

## What it does

- Indexes 1 or more folders, recursively. It skips every name that starts with a dot (such as `.git`) and never follows a symbolic link. A link loop therefore cannot trap a scan.
- Records each file's name, path, type, size, modified time, cloud state at once, all from the folder listing, with no file opened. Text extraction is slower, and it runs afterwards from a job queue.
- Never opens a file whose data is online only, such as a OneDrive or Dropbox file that is not on this device. Opening one would make the cloud service down-load it. See [Online-only files](#online-only-files).
- Reads online-only files on request: it down-loads each one, reads its text, then sets it back to online only, or keeps it on this device if you ask. A journal row records each file's earlier state before anything changes it, so after a crash or a restart, the next run still puts every file back. See [Reading online-only files](#reading-online-only-files). Windows only.
- Reads the text of plain-text files (UTF-8, with or without a byte order mark, or UTF-16 with one), the text layer of PDFs, the paragraphs of Word (`.docx`) and OpenDocument (`.odt`) files. Other files stay searchable by name, path, size, date.
- Indexes the entries of `.zip` files as rows of their own, with paths such as `backup.zip!/notes/may.txt`, and reads their text the same way. See [Zip files](#zip-files).
- Re-scans quickly. A file whose size and modified time have not changed keeps its row and its text, and no job is queued for it.
- Searches in 4 modes:
  - File name: a plain substring, where `%` and `_` are ordinary characters.
  - Full path: the same, over the whole path.
  - Text, by regular expression: PostgreSQL's own engine, line by line, like `grep`. Each hit lists up to 5 matching lines from the top of the file, with their line numbers.
  - Text, by words: PostgreSQL full-text search, with a short snippet per hit.
- Filters by folder (sub-folders included), file type, size range, modified date range, where the text came from (a text file, a PDF, a Word or OpenDocument file, no text at all), where the data is (on this disk, online only, not known). Every filter is 1 condition in the query, which lets them combine freely.
- Exports the results to CSV or JSON. Both files record the query and filters that produced them.
- Runs the job queue on PostgreSQL. Workers claim jobs with `FOR UPDATE SKIP LOCKED`. Several can therefore run at once, and no 2 ever take the same job.
- Puts a job back in the queue when its worker died: each run of the queue (`work`, `POST /jobs/run`) 1st re-queues every job that has been `running` for more than 30 minutes, with the reason in its error column. A job claimed 3 times fails for good, with "gave up after 3 attempts" as its reason. `work` prints both counts when either is above 0, and `POST /jobs/run` answers with `requeued` and `gave_up` beside `processed`.

## Who it is for

Anyone with a lot of files on their own drives who needs to find a document by what it says, not only by what it is called. A typical case is an old letter, receipt, note somewhere among thousands of folders.

## Built with

- Back-end: Python and FastAPI. Built and tested with Python 3.13.18, FastAPI 0.115.12, psycopg 3.2.9, pypdf 5.6.0. Zip files are read with `zipfile` from Python's standard library. The package needs Python 3.11 or later.
- Database: PostgreSQL with the `pg_trgm` extension. Built and tested with PostgreSQL 16.15. The schema uses generated columns, which need PostgreSQL 12 or later.
- Front-end: Next.js 16.4.3 with React 19.2.9 and TypeScript 5.9.3, built and tested with Node.js 22.23.4.
- Tests: pytest 8.4.0 with Hypothesis 6.135.0 for the back-end, and Vitest 3.2.4 for the front-end.

## Set-up

You need a PostgreSQL server that you can create databases on, Python 3.11 or later, Node.js 22.

Create the database:

```sh
createdb filescour
```

Start the API:

```sh
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
export FILESCOUR_DATABASE_URL=postgresql://localhost/filescour
uvicorn filescour.api:app --port 8000
```

Start the search screen in a 2nd terminal, then open http://localhost:3000:

```sh
cd frontend
npm ci
npm run dev
```

The screen calls the API at `http://localhost:8000`. Set `NEXT_PUBLIC_FILESCOUR_API` to use another address, and set `FILESCOUR_CORS_ORIGINS` on the API (a comma-separated list) when the screen runs anywhere other than `http://localhost:3000`.

### From the command line

The same steps work without the screen:

```sh
python -m filescour add ~/Documents   # prints the folder id
python -m filescour add /mnt/c/Users/me/Notes --extract-unknown
python -m filescour scan              # scans every folder
python -m filescour work              # extracts the queued text
python -m filescour roots             # lists the folders
python -m filescour fetch PATH...     # queues online-only files to read, then put back
python -m filescour fetch --keep PATH # queues them to read and keep on this device
python -m filescour fetch-run         # runs the queue (Windows only)
python -m filescour fetch-run --max-file-mb 50 --free-floor-mb 4096
```

## Tests

The back-end tests run against a real PostgreSQL, in a database that they wipe:

```sh
createdb filescour_test
cd backend
. .venv/bin/activate
export FILESCOUR_TEST_DATABASE_URL=postgresql://localhost/filescour_test
pytest
```

The main test checks every search against a plain scan of the same files. It builds a seeded random tree of 70 files, then Hypothesis draws up to 300 cases (a mode, a query, any mix of filters), and each search must return exactly the files and lines that a slow, obvious Python scan returns. Other tests cover the exact bounds of every filter, the export bytes, re-scans after changes, 4 workers draining 300 jobs at once. The online-only tests stand in for the Windows attributes, since Linux has none: they set each file's attributes, then check that no online-only file is ever opened, that a file is read once it comes down to the disk, that a worker checks again just before it opens a file. The down-load tests stand in for Windows and the sync app in the same way, and they check that the earlier state is saved before the first change, that a crash half-way is undone by the next run, that a file not yet back online keeps its journal row open, the size cap, the free-space floor. The zip tests build small zips on the fly. They check the entry paths and text, every search mode, both exports, re-scans of a changed zip and of a deleted one, that an online-only zip is never opened, each guard in [Zip files](#zip-files).

The front-end tests cover the form-to-query rules and how a zip entry is told apart from a file on the disk:

```sh
cd frontend
npm test
npm run typecheck
npm run build
```

On every push and pull request, GitHub Actions runs the back-end tests (`pytest` against a PostgreSQL 16 service) with Python 3.11 and 3.13. It also runs the front-end checks (`npm test`, `npm run typecheck`, `npm run build`) with Node.js 22 and 24 (see [.github/workflows/ci.yml](.github/workflows/ci.yml)).

## How search decides

- Name and path searches ignore case unless "Ignore case" is off. They match a plain substring, with no wildcards.
- A regular expression matches line by line. A file is a hit when at least 1 of its lines matches, and `^`, `$`, `\A`, `\Z` match at the start and end of each line. The syntax is PostgreSQL's ([POSIX regular expressions](https://www.postgresql.org/docs/current/functions-matching.html#FUNCTIONS-POSIX-REGEXP)).
- Word search uses PostgreSQL's `simple` text search configuration, with no stemming. It covers characters 1 to 500,000 of each file, which keeps every index entry under PostgreSQL's 1 MB limit. Name, path, regular-expression searches cover all of the stored text.
- Text is stored up to 10,000,000 characters per file, and per zip entry. A longer file is cut there and marked as cut in the results.
- The date filters work in UTC. The API's `modified_after` includes its moment and `modified_before` excludes its moment. On the screen, "Modified to" includes the whole day you pick.
- Results come in path order, up to 1,000 per search, with the total count of matches.

## Word and OpenDocument files

A `.docx` or `.odt` file is a zip of XML parts. A worker reads the paragraphs from those parts with the Python standard library, 1 line per paragraph, such that a regular expression search can show the matching lines. In a `.docx`, the body comes first, then the headers, footers, footnotes, endnotes. In an `.odt`, headings, list items, notes count as paragraphs, and runs of spaces, tabs, line breaks keep their place. The source filter calls this text `doc`.

| Case | What happens |
| --- | --- |
| An XML part over 64 MB uncompressed | The document gets no text. The size is checked before the part is read, and a part that holds more than it declares is cut off at the limit and refused. |
| A part with a DOCTYPE | Refused before it is parsed. Word and OpenDocument never write one, and it is the only place XML entities can be declared, so entity expansion cannot run. |
| A broken zip, damaged compressed data, a missing main part, XML that is not well-formed, a document with no text | The document keeps its name, path, size, date in the index without text. |

In each of these cases a document on the disk has its job marked failed, with the reason. A document inside a zip keeps its entry row without text, the same as a broken PDF entry.

A database made before this release gets the wider `content_source` check the next time the schema set-up runs.

## Zip files

A `.zip` file goes on the job queue like a text file. A worker opens it and adds a row for each entry, with the entry's name, type, uncompressed size, date. Text files, PDFs, Word files, OpenDocument files inside the zip get their text, read the same way as files on the disk.

- An entry's path is the zip's path, then `!/`, then its path inside the zip: `/home/user/old/backup.zip!/notes/may.txt`.
- Search by name, by path, by text finds entries the same way as files, and every filter applies to them. A folder filter on the zip's folder includes its entries.
- The API and the JSON export name the zip that holds each entry in the field `archive` (`null` for a file on the disk). The CSV export lists entries in the same columns as files.
- Folders inside a zip get no row of their own. Names that start with a dot are skipped, the same as in a scan.
- The zip itself counts as a cloud file. An online-only zip is never opened, and the worker checks again just before it opens one. Its entries take the zip's cloud state. A request to read online-only files leaves zip files out (they count as files with no text), and it never down-loads a zip entry on its own.
- A re-scan keeps the entries of an unchanged zip. A changed zip loses its old entries at that scan and goes on the queue again. A deleted zip takes its entries with it.
- When 2 entries have the same name, the 1st one keeps the row. A real file at the same path as an entry (inside a folder named `backup.zip!`) wins over the entry.
- When the date in the zip is not a valid date, the entry takes the zip file's own modified time.

The guards against hostile zips, such as zip bombs:

| Case | What happens |
|---|---|
| More than 10,000 entries | The zip is not expanded, and its job fails with the reason. |
| Text files, PDFs, Word files, OpenDocument files inside it add up to more than 256 MB uncompressed | The zip is not expanded, and its job fails with the reason. The sizes are checked before any entry is read. Other entries, such as photos, are never read, and their sizes do not count. |
| An encrypted entry | It gets a row with its name, size, date, but its data is never read. |
| An entry that holds more data than its header says | It gets a row, but no text. |
| A name that could point outside the zip: an absolute path, a drive letter such as `C:`, a `..` or `.` part, an empty part, a backslash | It gets no row. |
| A zip inside the zip | It gets a row, but its own entries are not indexed. |
| A file that is not a readable zip | Its job fails with "not a readable zip". The file is still found by name and path. |

## Online-only files

OneDrive, Dropbox and other services that use the Windows Cloud Files API keep a placeholder on disk for a file whose data is online. Opening or reading the placeholder makes the service down-load the file. filescour therefore reads each file's attributes from the folder listing (Python's `os.scandir`, which on Windows needs no extra system call per file and opens no file) and gives each file 1 of 3 states:

| State | When | Text read? |
|---|---|---|
| `local` | No recall or offline attribute is set. | Yes. |
| `online` | `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`, `FILE_ATTRIBUTE_RECALL_ON_OPEN` or `FILE_ATTRIBUTE_OFFLINE` is set. | Never. |
| `unknown` | The attributes cannot be read: filescour runs on Linux inside WSL, and the folder is on a Windows drive (a `drvfs` mount such as `/mnt/c`). | Only for a folder added with `--extract-unknown` (or `"extract_unknown": true`). |

The attribute values come from Microsoft's [File Attribute Constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants) page. An online file keeps its name, path, size, date in the index, so it is still found by name and path. A file that comes down to the disk is read at the next scan. A file that goes online after it was read keeps its text. Just before a worker opens a file, it reads the attributes again (on Windows with `FindFirstFileW`, which lists the file without opening it), and a file that went online since the scan is skipped, not read.

## Reading online-only files

On request, filescour reads files whose data is online only, in 1 of 2 modes:

| Mode | What happens | Afterwards |
|---|---|---|
| `temporary` (the default) | The file comes down, its text goes into the index, then the file is set back to online only. | The text is searchable, and the disk space is given back. |
| `keep` | The file comes down and its text goes into the index. | The file stays "always available" on this device. |

How a run works:

- A request only adds rows to a journal (the `fetches` table). A run does the work, at once or later ("Queue for later" on the screen).
- Before the first change to a file, its row records the file's attributes, and that write is committed.
- The changes use the commands that Microsoft documents for OneDrive [Files On-Demand states](https://learn.microsoft.com/en-us/onedrive/files-on-demand-windows): `attrib +p` (always available, which down-loads the file), then `attrib -p` and `attrib +u` (online only, which frees the space).
- A row in mode `temporary` closes only once the file reads as online only again. A run starts with any rows left open, which puts files back before new ones come down.
- A run has 3 limits, each one configurable:

  | Limit | Default | Environment setting | Per run |
  |---|---|---|---|
  | Space to leave free on the drive. A run stops before a down-load would go under it. | 2048 MB | `FILESCOUR_FETCH_FREE_FLOOR_MB` | `free_floor_mb`, `--free-floor-mb` |
  | Largest file to down-load. A larger file is skipped. | 500 MB | `FILESCOUR_FETCH_MAX_FILE_MB` | `max_file_mb`, `--max-file-mb` |
  | Wait for 1 file to come down or go back. | 120 seconds | `FILESCOUR_FETCH_WAIT_SECONDS` | `wait_seconds`, `--wait-seconds` |

  A value set for 1 run (an API parameter, a command-line option, a field on the screen) wins over the environment, and the environment wins over the default. Each value is a whole number, 0 or more.
- 1 run at a time: a 2nd run that starts while 1 is going stops at once. 2 runs could otherwise set a file back to online only while the other is still reading it.

## API

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Reports the version. |
| GET | `/roots` | Lists the folders, with counts of files, online-only files, files whose cloud state is not known, zip entries. |
| POST | `/roots` | Adds a folder. Body: `{"path": "/home/jon/Documents", "extract_unknown": false}`. |
| DELETE | `/roots/{id}` | Removes a folder and its rows from the index. The files on disk are not touched. |
| POST | `/roots/{id}/scan` | Scans a folder and queues text extraction. |
| GET | `/jobs` | Counts jobs by state: queued, running, done, failed, skipped. |
| POST | `/jobs/run?limit=N` | Runs up to N queued jobs. |
| GET | `/fetch` | Counts the journal rows by state, with the size of the queued files and the current limits. |
| POST | `/fetch` | Queues online-only files to read. Body: `{"paths": ["/path/a.txt"], "mode": "temporary"}`. |
| POST | `/fetch/run?limit=N` | Runs up to N journal rows (Windows only). Optional: `free_floor_mb`, `max_file_mb`, `wait_seconds`. Answers with this run's counts and the journal `totals`. |
| GET | `/search` | Searches. Parameters: `q`, `mode`, `ignore_case`, `limit`, `sort`, `folder`, `ext` (repeatable), `min_size`, `max_size`, `modified_after`, `modified_before`, `source`, `cloud`. `sort` takes `path` (the default), `name`, `size`, `modified`, each with an optional leading `-` for high to low. A tie falls back to the path, A to Z. |
| GET | `/export?format=csv` | The same search as a CSV or JSON file. |

## Sample export

These lines are pasted from a real run on a made-up folder with 2 files (a letter and a receipt), searching the text for `4417`:

```text
$ curl "http://localhost:8000/export?format=csv&q=4417&mode=regex"
# filescour search: {"filters": {}, "ignore_case": true, "mode": "regex", "query": "4417", "returned": 2, "total": 2}
path,name,ext,size,modified,source,cloud,line,text
/tmp/dsdemo/letters/may.txt,may.txt,txt,68,2026-11-08T23:11:27.779114+00:00,file,local,3,Invoice 4417 attached.
/tmp/dsdemo/receipts/may.csv,may.csv,csv,29,2026-11-08T23:11:27.779114+00:00,file,local,2,"2026-05-01,4417"
```

## Limits today

- The online-only check reads real Windows attributes only when filescour runs on Windows. That code path has not been run yet (the tests here use a stand-in), and the tool has not been tried on a real OneDrive or Dropbox folder.
- Inside WSL, every file on a Windows drive is `unknown`, and its text is not read unless its folder allows it. Reading the Windows attributes from WSL is item 10 in [ROADMAP.md](ROADMAP.md).
- Reading online-only files runs only on Windows, and it uses the OneDrive commands above. It has not been run on Windows yet (the tests use a stand-in), and it is not known whether Dropbox acts on the same commands.
- A down-load run happens inside 1 request to the API, which is slow for many files. A background worker is item 1 in [ROADMAP.md](ROADMAP.md).
- Scanned PDFs and images have no text layer, and only their names, paths, sizes, dates are searchable.
- The older binary Word format (`.doc`) and the other OpenDocument types (`.ods`, `.odp`) have no text reader yet.
- Only `.zip` archives are expanded. A zip inside a zip is listed as an entry, but its own entries are not indexed.
- A zip stores each entry's date and time with no time zone, and filescour reads it as UTC. An entry's date can therefore be off by the time zone offset of the computer that made the zip.
- It has been built and tested on Linux only.

## Where it runs

- The API and the scans run wherever Python 3.11 or later and PostgreSQL run, on the computer that holds the files. Only Linux has been tried.
- The screen is a web page. Any current browser that can reach the API can use it, a phone's included. It was checked at a phone width of 390 pixels, where the page does not scroll sideways.
- The online-only guard and the down-loads work with Windows file attributes only. On Linux and macOS, every file reads as on this disk, except files on a Windows drive seen from WSL.

## Licence

MIT Licence. See [LICENSE](LICENSE).
