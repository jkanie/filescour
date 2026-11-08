"""The down-load journal: read online-only files, then put them back.

Linux has no Cloud Files API, so FakeOps stands in for Windows and the sync
app: "attrib +p" brings a file down after a few polls, and "attrib +u" sends
it back online after a few polls, the way a sync app works in the background.
The test files are real files on disk, which extract() reads.
"""

import os

import pytest

from filescour import cloud, db, fetch, indexer, jobs
from filescour.search import Filters, search

from .helpers import write

ONLINE = cloud.RECALL_ON_DATA_ACCESS | cloud.UNPINNED
LOCAL = 0


class FakeOps:
    def __init__(self, attrs: dict[str, int], lag: int = 2, free: int = 10**12):
        self.attrs = attrs
        self.lag = lag
        self.free = free
        self.pending: dict[str, list] = {}  # path -> [polls left, attrs after]
        self.calls: list[tuple[str, str]] = []
        self.never_back: set[str] = set()  # paths the sync app never frees
        self.on_pin = None

    def attributes(self, path):
        p = self.pending.get(path)
        if p is not None:
            p[0] -= 1
            if p[0] <= 0:
                self.attrs[path] = p[1]
                del self.pending[path]
        return self.attrs.get(path)

    def pin(self, path):
        self.calls.append(("pin", path))
        if self.on_pin:
            self.on_pin(path)
        self.pending[path] = [self.lag, cloud.PINNED]

    def unpin(self, path):
        self.calls.append(("unpin", path))
        if path in self.never_back:
            return
        self.pending[path] = [self.lag, ONLINE]

    def free_bytes(self, path):
        return self.free

    def sleep(self, seconds):
        pass


@pytest.fixture
def online_tree(conn, tmp_path, monkeypatch):
    """3 text files and 1 photo. a.txt and b.txt are online only."""
    attrs: dict[str, int] = {}
    monkeypatch.setattr(cloud, "attributes_of", lambda st, path: attrs.get(path, LOCAL))
    a = write(tmp_path / "a.txt", "alpha invoice 4417\n")
    b = write(tmp_path / "b.txt", "bravo\n")
    c = write(tmp_path / "c.txt", "charlie\n")
    p = write(tmp_path / "p.jpg", b"\xff\xd8\x00")
    for f in (a, b, p):
        attrs[str(f)] = ONLINE
    rid = indexer.add_root(conn, str(tmp_path))
    r = indexer.scan_root(conn, rid)
    assert (r.queued, r.held) == (1, 2)
    jobs.run(conn)
    return attrs, {k: str(v) for k, v in dict(a=a, b=b, c=c, p=p).items()}


def rows(conn):
    return conn.execute(
        "SELECT path, mode, state, earlier_attrs, note FROM fetches ORDER BY id"
    ).fetchall()


def test_request_queues_only_online_text_files(conn, online_tree):
    _, f = online_tree
    got = fetch.request(conn, [f["a"], f["b"], f["c"], f["p"], "/no/such"], "temporary")
    assert (got.queued, got.not_online, got.no_text) == (2, 1, 1)
    assert got.bytes == os.path.getsize(f["a"]) + os.path.getsize(f["b"])
    # Asking again adds nothing while the rows are open.
    assert fetch.request(conn, [f["a"]], "keep").queued == 0
    with pytest.raises(ValueError):
        fetch.request(conn, [f["a"]], "later")


def test_temporary_reads_the_text_then_puts_the_file_back(conn, online_tree):
    attrs, f = online_tree
    ops = FakeOps(attrs)
    fetch.request(conn, [f["a"]], "temporary")
    r = fetch.run(conn, ops)
    assert (r.done, r.skipped, r.waiting, r.stopped) == (1, 0, 0, None)
    assert ops.calls == [("pin", f["a"]), ("unpin", f["a"])]
    assert cloud.state_from_attributes(attrs[f["a"]]) == "online"
    assert rows(conn) == [(f["a"], "temporary", "done", ONLINE, None)]
    (hit,) = search(conn, "4417", "regex").hits
    assert (hit.path, hit.cloud, hit.source) == (f["a"], "online", "file")


def test_keep_leaves_the_file_on_this_device(conn, online_tree):
    attrs, f = online_tree
    ops = FakeOps(attrs)
    fetch.request(conn, [f["b"]], "keep")
    assert fetch.run(conn, ops).done == 1
    assert ops.calls == [("pin", f["b"])]
    assert attrs[f["b"]] == cloud.PINNED
    (hit,) = search(conn, "bravo", "words").hits
    assert hit.cloud == "local"


def test_the_earlier_state_is_saved_before_anything_changes(conn, online_tree, test_url):
    attrs, f = online_tree
    ops = FakeOps(attrs)
    seen = []

    def look(path):  # runs inside "attrib +p", before the file changes
        with db.connect(test_url) as other:
            seen.append(other.execute(
                "SELECT state, earlier_attrs FROM fetches").fetchone())

    ops.on_pin = look
    fetch.request(conn, [f["a"]], "temporary")
    fetch.run(conn, ops)
    assert seen == [("downloading", ONLINE)]


def test_a_crash_after_the_down_load_is_undone_by_the_next_run(conn, online_tree):
    attrs, f = online_tree
    ops = FakeOps(attrs)

    def crash(path):
        raise KeyboardInterrupt  # the process dies just as the file comes down

    ops.on_pin = crash
    fetch.request(conn, [f["a"]], "temporary")
    with pytest.raises(KeyboardInterrupt):
        fetch.run(conn, ops)
    attrs[f["a"]] = cloud.PINNED  # the sync app finished the down-load anyway
    assert rows(conn)[0][2] == "downloading"
    # The restart.
    ops2 = FakeOps(attrs)
    r = fetch.run(conn, ops2)
    assert (r.done, r.waiting) == (1, 0)
    assert cloud.state_from_attributes(attrs[f["a"]]) == "online"
    assert search(conn, "4417", "regex").hits[0].cloud == "online"


def test_a_file_not_yet_back_online_keeps_its_row_open(conn, online_tree):
    attrs, f = online_tree
    ops = FakeOps(attrs)
    ops.never_back.add(f["a"])
    fetch.request(conn, [f["a"]], "temporary")
    r = fetch.run(conn, ops, lim=fetch.Limits(0, 500, 3))
    assert (r.done, r.waiting) == (0, 1)
    assert rows(conn)[0][2:] == ("restoring", ONLINE, "not confirmed online only yet")
    # The next run tries again first, and closes the row once it is confirmed.
    ops2 = FakeOps(attrs)
    r = fetch.run(conn, ops2)
    assert (r.done, r.waiting) == (1, 0)
    assert ops2.calls == [("unpin", f["a"])]
    assert rows(conn)[0][2] == "done"


def test_size_cap_and_free_space_floor(conn, online_tree, tmp_path):
    attrs, f = online_tree
    big = write(tmp_path / "big.txt", "x" * (fetch.MB + 1))  # just over 1 MB
    attrs[str(big)] = ONLINE
    indexer.scan_root(conn, conn.execute("SELECT id FROM roots").fetchone()[0])
    fetch.request(conn, [str(big), f["b"]], "temporary")
    r = fetch.run(conn, FakeOps(attrs), lim=fetch.Limits(0, 1, 120))
    assert (r.done, r.skipped) == (1, 1)  # big.txt is over the 1 MB cap, b.txt is not
    got = {r[0]: r[2:] for r in rows(conn)}
    assert got[str(big)] == ("skipped", None, "larger than 1 MB")
    fetch.request(conn, [f["a"]], "temporary")
    ops = FakeOps(attrs, free=2 * fetch.MB)
    r = fetch.run(conn, ops, lim=fetch.Limits(2, 500, 120))
    assert r.stopped == "free space would fall under 2 MB"
    assert ops.calls == []
    assert fetch.summary(conn)["queued"] == 1


def test_a_file_already_on_disk_is_read_and_left_alone(conn, online_tree):
    attrs, f = online_tree
    fetch.request(conn, [f["a"]], "temporary")
    attrs[f["a"]] = cloud.PINNED  # brought down by hand since the request
    ops = FakeOps(attrs)
    assert fetch.run(conn, ops).done == 1
    assert ops.calls == []
    assert rows(conn)[0][2:] == ("done", cloud.PINNED, "already on this disk")


def test_1_run_at_a_time(conn, online_tree, test_url):
    attrs, f = online_tree
    fetch.request(conn, [f["a"]], "temporary")
    with db.connect(test_url) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (fetch.LOCK_KEY,))
        r = fetch.run(conn, FakeOps(attrs))
        assert r.stopped == "another run is in progress"
        other.execute("SELECT pg_advisory_unlock(%s)", (fetch.LOCK_KEY,))
    assert fetch.run(conn, FakeOps(attrs)).done == 1


def test_summary_counts_and_queued_bytes(conn, online_tree):
    attrs, f = online_tree
    fetch.request(conn, [f["a"], f["b"]], "temporary")
    s = fetch.summary(conn)
    assert (s["queued"], s["done"]) == (2, 0)
    assert s["queued_bytes"] == os.path.getsize(f["a"]) + os.path.getsize(f["b"])


@pytest.mark.skipif(os.name == "nt", reason="checks the refusal off Windows")
def test_off_windows_the_run_is_refused(conn):
    with pytest.raises(fetch.FetchUnavailable, match="only when filescour runs on Windows"):
        fetch.run(conn)


def test_search_filter_still_finds_fetched_text_as_online(conn, online_tree):
    attrs, f = online_tree
    fetch.request(conn, [f["a"], f["b"]], "temporary")
    fetch.run(conn, FakeOps(attrs))
    names = [h.name for h in search(conn, "", "name", Filters(cloud="online", source="file")).hits]
    assert names == ["a.txt", "b.txt"]


def test_files_left_down_go_back_before_new_ones_come_down(conn, online_tree):
    attrs, f = online_tree
    fetch.request(conn, [f["b"]], "temporary")  # row 1, queued
    fetch.request(conn, [f["a"]], "temporary")  # row 2
    # Row 2 as a crash left it: a.txt down-loaded, not yet put back.
    conn.execute("UPDATE fetches SET state = 'restoring', earlier_attrs = %s "
                 "WHERE path = %s", (ONLINE, f["a"]))
    attrs[f["a"]] = cloud.PINNED
    ops = FakeOps(attrs, free=0)  # no room for anything new
    r = fetch.run(conn, ops, lim=fetch.Limits(1, 500, 120))
    assert r.stopped == "free space would fall under 1 MB"
    assert ops.calls == [("unpin", f["a"])]
    assert [(p, s) for p, _, s, *_ in rows(conn)] == [(f["b"], "queued"), (f["a"], "done")]


def test_the_api(conn, online_tree, monkeypatch):
    from fastapi.testclient import TestClient

    from filescour.api import app

    attrs, f = online_tree
    with TestClient(app) as c:
        r = c.post("/fetch", json={"paths": [f["a"], f["c"]], "mode": "temporary"})
        assert r.status_code == 200
        assert r.json() == {"queued": 1, "bytes": os.path.getsize(f["a"]),
                            "not_online": 1, "no_text": 0}
        assert c.get("/fetch").json()["queued"] == 1
        assert c.post("/fetch", json={"paths": [], "mode": "temporary"}).status_code == 422
        assert c.post("/fetch", json={"paths": [f["a"]], "mode": "later"}).status_code == 422
        assert c.get("/fetch").json()["limits"] == {
            "free_floor_mb": 2048, "max_file_mb": 500, "wait_seconds": 120}
        assert c.post("/fetch/run", params={"max_file_mb": -1}).status_code == 422
        if os.name != "nt":
            r = c.post("/fetch/run", params={"free_floor_mb": 100, "wait_seconds": 30})
            assert r.status_code == 409
            assert "only when filescour runs on Windows" in r.json()["detail"]
        # With Windows stood in: this run's counts, then the journal totals.
        monkeypatch.setattr(fetch, "default_ops", lambda: FakeOps(attrs))
        fetch.request(conn, [f["b"]], "temporary")
        r = c.post("/fetch/run", params={"max_file_mb": 0}).json()
        assert (r["done"], r["skipped"], r["waiting"], r["stopped"]) == (0, 2, 0, None)
        r = c.post("/fetch/run").json()
        assert (r["done"], r["skipped"]) == (0, 0)
        assert (r["totals"]["done"], r["totals"]["skipped"]) == (0, 2)


def test_limits_come_from_overrides_then_the_environment_then_defaults(monkeypatch):
    for env in fetch.ENVS.values():
        monkeypatch.delenv(env, raising=False)
    assert fetch.limits() == fetch.Limits(2048, 500, 120)
    monkeypatch.setenv(fetch.FREE_FLOOR_ENV, "4096")
    monkeypatch.setenv(fetch.MAX_FILE_ENV, " 50 ")
    assert fetch.limits() == fetch.Limits(4096, 50, 120)
    assert fetch.limits(max_file_mb=0, wait_seconds=5) == fetch.Limits(4096, 0, 5)
    assert fetch.limits().free_floor == 4096 * fetch.MB
    monkeypatch.setenv(fetch.WAIT_ENV, "2m")
    with pytest.raises(ValueError, match="FILESCOUR_FETCH_WAIT_SECONDS must be a whole number"):
        fetch.limits()
    with pytest.raises(ValueError, match="free_floor_mb must be 0 or more"):
        fetch.limits(free_floor_mb=-1, wait_seconds=1)


def test_command_line_limit_options():
    from filescour.__main__ import _options
    assert _options([]) == {}
    assert _options(["--free-floor-mb", "100", "--wait-seconds", "30"]) == {
        "free_floor_mb": 100, "wait_seconds": 30}
    for bad in (["--max-file-mb"], ["--max-file-mb", "-1"], ["--size", "3"]):
        assert _options(bad) is None
