# Roadmap

Planned work after the seed, with an hour estimate per item. The total is 37.5 hours.

## 1. CI with a real PostgreSQL, 2 hours

- What: a GitHub Actions work-flow that starts a PostgreSQL service, runs `pytest` on the back-end, then runs `npm test`, `npm run typecheck`, `npm run build` on the front-end, on every push and pull request.
- Why: the back-end tests need a live database. A run on a clean machine is therefore the only proof that the set-up steps in the README are complete.

## 2. Files inside zip archives, 3 hours

- What: index the entries of `.zip` files as their own rows, with paths such as `backup.zip!/notes/may.txt`, including their text.
- Why: old backups and e-mailed folders often sit in zips, and today, only the zip's own name is searchable.

## 3. Word and OpenDocument text, 2 hours

- What: extract the text of `.docx` and `.odt` files (both are zip archives of XML), with tests built from small generated documents.
- Why: letters and reports are mostly Word files, and today, they are searchable by name only.

## 4. Stuck jobs and a re-try limit, 1.5 hours

- What: put a job that has stayed in `running` past a time-out back in the queue, and fail it for good after 3 attempts, with the reason kept.
- Why: today, a worker that dies mid-job leaves its job in `running` forever, and that file's text is never extracted.

## 5. Background worker with progress, 2 hours

- What: a separate worker process (`python -m filescour worker`), with the screen polling the job counts to show progress and a button to cancel the queued jobs.
- Why: the screen's "Extract queued text" button runs up to 1,000 jobs inside 1 request today, which is slow on a big folder.

## 6. Highlighted matches, 1.5 hours

- What: return the position of each match within its line, and mark it on the screen.
- Why: in a long line, the matching words are hard to spot.

## 7. Paging and sorting, 1.5 hours

- What: page through results past number 1,000, and sort by name, size, modified date as well as by path.
- Why: a broad search on a big drive returns more than 1,000 files today, and the rest cannot be reached.

## 8. File preview, 2 hours

- What: a side panel with the stored text of the selected file, scrolled to its earliest match.
- Why: today, a hit shows at most 5 lines, and reading more means opening the file elsewhere.

## 9. Saved searches, 2 hours

- What: name and store a search with its filters in PostgreSQL, list the saved searches, re-run one in 1 click.
- Why: a search that is run every week (new receipts, a project's notes) has to be typed in again each time.

## 10. End-to-end tests of the screen, 2 hours

- What: Playwright tests that add a folder, scan it, extract its text, search in every mode, export, run in CI against the real API and database.
- Why: the unit tests cover the form rules, but only an end-to-end run proves that the screen and the API agree.

## 11. OneDrive search through Microsoft Graph, 4 hours

- What: an optional connector that searches OneDrive with Microsoft Graph (`Files.Read` only) and merges its hits, marked as found by OneDrive. Tests use recorded responses.
- Why: Graph searches the text of files that were never down-loaded, with no down-load at all.

## 12. Watch mode, 2.5 hours

- What: re-scan a folder when its files change, using file-system change events, with a short delay that groups bursts of changes.
- Why: today, the index is only as fresh as the last scan someone started.

## 13. Word search over the whole text, 2 hours

- What: split each file's text into chunks for word search, such that words past character 500,000 are found too.
- Why: the 500,000-character limit keeps every index entry under PostgreSQL's 1 MB limit, but it hides words at the end of long files.

## 14. Windows attributes from inside WSL, 3 hours

- What: for a folder on a Windows drive under WSL, read each file's attributes through Windows (for example with a small helper that WSL's Windows interop runs once per folder), then mark each file `local` or `online` instead of `unknown`.
- Why: today, inside WSL, every file on a Windows drive such as `/mnt/c` is `unknown`, and its text is not read unless the whole folder allows it. An allowed folder then gives no protection for its online-only files.

## 15. Down-load time estimate for online-only hits, 1.5 hours

- What: before any down-load run, show the count and total size of the chosen online-only files, with an estimated time from the rate measured on the most recent down-loads.
- Why: a down-load of many GB can take hours. The choice to go ahead needs the size and the time up front.

## 16. macOS online-only files, 3 hours

- What: find out how OneDrive and Dropbox mark online-only files on macOS, then give those files the same guard and the same down-load journal as on Windows.
- Why: today, on a Mac, every file reads as on this disk, so the guard does not protect a cloud folder there.

## 17. Release v0.1.0, 2 hours

- What: a change log, a tag, a Docker Compose file that starts PostgreSQL, the API, the screen together.
- Why: the set-up takes 3 programs today, and 1 command lowers the bar for trying it.

## Total

37.5 hours.
