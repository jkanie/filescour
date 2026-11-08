"""The online-only guard.

Linux has no Windows file attributes, so these tests stand in for them: they
replace cloud.attributes_of (what the scan reads from the folder listing) and
cloud.current_state (what a worker reads just before it opens a file). The
real Windows calls run only on Windows.
"""

import pytest

from filescour import cloud, indexer, jobs
from filescour.search import Filters, search

from .helpers import write

ONLINE = cloud.RECALL_ON_DATA_ACCESS | cloud.UNPINNED
PINNED = cloud.PINNED


@pytest.fixture
def fake_windows(monkeypatch):
    """Attributes by path. A path not listed reads as a plain local file (0)."""
    attrs: dict[str, int] = {}
    monkeypatch.setattr(cloud, "attributes_of", lambda st, path: attrs.get(path, 0))

    def now(path):
        a = attrs.get(path, 0)
        return cloud.state_from_attributes(a), a

    monkeypatch.setattr(cloud, "current_state", now)
    return attrs


@pytest.fixture
def opened(monkeypatch):
    """Every path that a worker opens for its text."""
    seen: list[str] = []
    real = jobs.extract

    def spy(path, ext):
        seen.append(str(path))
        return real(path, ext)

    monkeypatch.setattr(jobs, "extract", spy)
    return seen


@pytest.mark.parametrize("bits, state", [
    (0, "local"),
    (PINNED, "local"),
    (cloud.UNPINNED, "local"),
    (cloud.RECALL_ON_DATA_ACCESS, "online"),
    (cloud.RECALL_ON_OPEN, "online"),
    (cloud.OFFLINE, "online"),
    (cloud.RECALL_ON_DATA_ACCESS | cloud.UNPINNED, "online"),
])
def test_state_from_attributes(bits, state):
    assert cloud.state_from_attributes(bits) == state


def test_online_files_are_listed_but_never_opened(conn, tmp_path, fake_windows, opened):
    local = write(tmp_path / "here.txt", "kept on disk\n")
    away = write(tmp_path / "away.txt", "only in the cloud\n")
    fake_windows[str(away)] = ONLINE
    rid = indexer.add_root(conn, str(tmp_path))
    r = indexer.scan_root(conn, rid)
    assert (r.added, r.queued, r.held) == (2, 1, 1)
    jobs.run(conn)
    assert opened == [str(local)]
    assert [h.name for h in search(conn, "", "name").hits] == ["away.txt", "here.txt"]
    (hit,) = search(conn, "", "name", Filters(cloud="online")).hits
    assert (hit.name, hit.cloud, hit.source) == ("away.txt", "online", None)
    assert search(conn, "cloud", "regex").hits == []


def test_online_wins_over_extract_unknown(conn, tmp_path, fake_windows, opened):
    away = write(tmp_path / "away.txt", "x")
    fake_windows[str(away)] = ONLINE
    rid = indexer.add_root(conn, str(tmp_path), extract_unknown=True)
    assert indexer.scan_root(conn, rid).held == 1
    jobs.run(conn)
    assert opened == []


def test_a_file_that_comes_down_is_read_on_the_next_scan(conn, tmp_path, fake_windows):
    away = write(tmp_path / "away.txt", "now on disk\n")
    fake_windows[str(away)] = ONLINE
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    fake_windows[str(away)] = PINNED  # "always keep on this device"
    r = indexer.scan_root(conn, rid)
    assert (r.unchanged, r.queued, r.held) == (1, 1, 0)
    jobs.run(conn)
    (hit,) = search(conn, "^now", "regex").hits
    assert (hit.cloud, hit.lines[0].text) == ("local", "now on disk")
    # A 3rd scan queues nothing more.
    assert indexer.scan_root(conn, rid).queued == 0


def test_a_file_that_goes_online_keeps_its_text(conn, tmp_path, fake_windows):
    p = write(tmp_path / "a.txt", "read while local\n")
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    jobs.run(conn)
    fake_windows[str(p)] = ONLINE
    r = indexer.scan_root(conn, rid)
    assert (r.unchanged, r.queued, r.held) == (1, 0, 0)
    (hit,) = search(conn, "local", "regex").hits
    assert hit.cloud == "online"


def test_the_worker_checks_again_before_it_opens(conn, tmp_path, fake_windows, opened):
    p = write(tmp_path / "a.txt", "x")
    rid = indexer.add_root(conn, str(tmp_path))
    assert indexer.scan_root(conn, rid).queued == 1
    fake_windows[str(p)] = ONLINE  # it went online-only after the scan
    jobs.run(conn)
    assert opened == []
    state, error = conn.execute("SELECT state, error FROM jobs").fetchone()
    assert (state, error) == ("skipped", "not opened: online only")
    assert jobs.summary(conn)["skipped"] == 1
    (cloud_state,) = conn.execute("SELECT cloud FROM files").fetchone()
    assert cloud_state == "online"
    # Back on disk: the next scan queues it again, and it is read.
    fake_windows[str(p)] = 0
    assert indexer.scan_root(conn, rid).queued == 1
    jobs.run(conn)
    assert opened == [str(p)]


def test_a_changed_online_file_loses_its_old_text_and_is_not_read(conn, tmp_path, fake_windows):
    p = write(tmp_path / "a.txt", "old\n", mtime_ns=1_700_000_000_000_000_000)
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    jobs.run(conn)
    write(p, "newer text\n", mtime_ns=1_700_000_100_000_000_000)
    fake_windows[str(p)] = ONLINE
    r = indexer.scan_root(conn, rid)
    assert (r.changed, r.queued, r.held) == (1, 0, 1)
    assert search(conn, "old", "regex").hits == []
    assert jobs.summary(conn)["queued"] == 0


def test_a_queued_job_is_dropped_when_its_file_changes_and_goes_online(conn, tmp_path,
                                                                     fake_windows):
    p = write(tmp_path / "a.txt", "one\n", mtime_ns=1_700_000_000_000_000_000)
    rid = indexer.add_root(conn, str(tmp_path))
    assert indexer.scan_root(conn, rid).queued == 1
    write(p, "two\n", mtime_ns=1_700_000_100_000_000_000)
    fake_windows[str(p)] = ONLINE
    assert indexer.scan_root(conn, rid).held == 1
    assert jobs.summary(conn)["queued"] == 0


# --- A Windows drive seen from WSL ------------------------------------------

WSL_MOUNTS = (
    "none /mnt/wsl tmpfs rw,relatime 0 0\n"
    "C:\\134 /mnt/c 9p rw,noatime,dirsync,aname=drvfs;path=C:\\;uid=1000;"
    "gid=1000;symlinkroot=/mnt/,mmap,access=client,msize=65536,trans=fd,"
    "rfd=5,wfd=5 0 0\n"
    "D: /mnt/d drvfs rw,noatime 0 0\n"
    "E: /mnt/my\\040drive drvfs rw 0 0\n"
    "/dev/sdc / ext4 rw,relatime 0 0\n"
)


def test_windows_drives_are_found_in_proc_mounts():
    assert cloud.parse_windows_drives(WSL_MOUNTS) == ["/mnt/my drive", "/mnt/c", "/mnt/d"]
    drives = cloud.parse_windows_drives(WSL_MOUNTS)
    assert cloud.under("/mnt/c/Users/me/OneDrive/a.txt", drives)
    assert cloud.under("/mnt/my drive/a.txt", drives)
    assert not cloud.under("/mnt/cdrom/a.txt", drives)
    assert not cloud.under("/home/me/a.txt", drives)
    assert cloud.parse_windows_drives("/dev/sda / ext4 rw 0 0\n") == []


def test_unknown_files_are_not_read_unless_the_folder_allows_it(conn, tmp_path, monkeypatch,
                                                                 opened):
    monkeypatch.setattr(cloud, "windows_drives", lambda: [str(tmp_path)])
    write(tmp_path / "a" / "doc.txt", "on a Windows drive\n")
    write(tmp_path / "b" / "doc.txt", "also on it\n")
    guarded = indexer.add_root(conn, str(tmp_path / "a"))
    allowed = indexer.add_root(conn, str(tmp_path / "b"), extract_unknown=True)
    assert indexer.scan_root(conn, guarded).held == 1
    assert indexer.scan_root(conn, allowed).queued == 1
    jobs.run(conn)
    assert opened == [str(tmp_path / "b" / "doc.txt")]
    got = {h.path: (h.cloud, h.source) for h in search(conn, "", "name").hits}
    assert got == {str(tmp_path / "a" / "doc.txt"): ("unknown", None),
                   str(tmp_path / "b" / "doc.txt"): ("unknown", "file")}


def test_bad_cloud_filter(conn):
    from filescour.search import BadQuery
    with pytest.raises(BadQuery, match="cloud must be"):
        search(conn, "", "name", Filters(cloud="maybe"))
