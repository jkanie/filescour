"""Tell whether a file's data is on this disk, without opening the file.

OneDrive, Dropbox and other services that use the Windows Cloud Files API keep
a placeholder on disk for a file whose data is still online. Opening or
reading such a file makes the service down-load it. The scan therefore reads
each file's attributes from the folder listing, which on Windows needs no
system call per file (see os.DirEntry.stat), and text extraction never opens
a file whose data is online only.

Each file gets 1 of 3 states:

- local: the data is on this disk.
- online: the data is online only (or in offline storage). Never opened.
- unknown: the attributes cannot be read. This happens when the program runs
  on Linux inside WSL and the folder is on a Windows drive (such as /mnt/c),
  since Linux sees no Windows attributes there. These files are not opened
  either, unless their folder was added with extract_unknown.
"""

from __future__ import annotations

import os

# Values from Microsoft's "File Attribute Constants" page:
# https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants
OFFLINE = 0x0000_1000
RECALL_ON_OPEN = 0x0004_0000
PINNED = 0x0008_0000
UNPINNED = 0x0010_0000
RECALL_ON_DATA_ACCESS = 0x0040_0000
ONLINE_BITS = OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS

STATES = ("local", "online", "unknown")

MOUNTS_FILE = "/proc/mounts"


def state_from_attributes(attrs: int) -> str:
    return "online" if attrs & ONLINE_BITS else "local"


def _unescape(field: str) -> str:
    # /proc/mounts writes a space as \040, a tab as \011, a backslash as \134.
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def parse_windows_drives(mounts_text: str) -> list[str]:
    """Mount points of Windows drives, as WSL lists them in /proc/mounts.

    WSL mounts a drive with the drvfs file system type, or with 9p and the
    option aname=drvfs. The longest mount points come first.
    """
    found = []
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        point, fstype, options = _unescape(parts[1]), parts[2], parts[3]
        if fstype == "drvfs" or (fstype == "9p" and "aname=drvfs" in options):
            found.append(point.rstrip("/") or "/")
    return sorted(set(found), key=lambda m: (-len(m), m))


def windows_drives() -> list[str]:
    """The Windows drive mounts of this machine. Empty outside WSL."""
    if os.name == "nt":
        return []
    try:
        with open(MOUNTS_FILE, encoding="utf-8", errors="replace") as f:
            return parse_windows_drives(f.read())
    except OSError:
        return []


def under(path: str, mounts: list[str]) -> bool:
    for m in mounts:
        if m == "/" or path == m or path.startswith(m + "/"):
            return True
    return False


def attributes_of(st: os.stat_result, path: str) -> int | None:
    """Windows file attributes of a stat result, or None off Windows."""
    return getattr(st, "st_file_attributes", None)


def state_of(st: os.stat_result, path: str, drives: list[str]) -> tuple[str, int | None]:
    """(state, Windows attributes) of a file found by the scan."""
    attrs = attributes_of(st, path)
    if attrs is not None:
        return state_from_attributes(attrs), attrs
    if under(path, drives):
        return "unknown", None
    return "local", None


def _find_attributes(path: str) -> int | None:
    """Windows only: the attributes from the folder listing of 1 file.

    FindFirstFileW lists the file without opening it, which is what the
    scan's folder listing does too.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FindFirstFileW.argtypes = [wintypes.LPCWSTR,
                                        ctypes.POINTER(wintypes.WIN32_FIND_DATAW)]
    kernel32.FindFirstFileW.restype = wintypes.HANDLE
    kernel32.FindClose.argtypes = [wintypes.HANDLE]
    data = wintypes.WIN32_FIND_DATAW()
    handle = kernel32.FindFirstFileW(path, ctypes.byref(data))
    if handle is None or handle == wintypes.HANDLE(-1).value:
        return None
    kernel32.FindClose(handle)
    return data.dwFileAttributes


def current_state(path: str) -> tuple[str, int | None]:
    """(state, attributes) of a file right now, read without opening it.

    Text extraction calls this just before it opens a file, in case the file
    went online-only after the scan.
    """
    if os.name == "nt":
        attrs = _find_attributes(path)
        if attrs is None:
            return "unknown", None
        return state_from_attributes(attrs), attrs
    if under(path, windows_drives()):
        return "unknown", None
    return "local", None


def may_read(state: str, extract_unknown: bool) -> bool:
    """Whether text extraction may open a file in this state."""
    return state == "local" or (state == "unknown" and extract_unknown)
