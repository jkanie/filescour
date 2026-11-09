import { describe, expect, it } from "vitest";

import { baseName, downloadable, zipEntry } from "./hits";

describe("zipEntry", () => {
  it("splits an entry's path into its zip and its path inside the zip", () => {
    expect(
      zipEntry({ path: "/home/jon/backup.zip!/notes/may.txt", archive: "/home/jon/backup.zip" }),
    ).toEqual({ zip: "/home/jon/backup.zip", entry: "notes/may.txt" });
  });

  it("goes by the archive field, not by the look of the path", () => {
    expect(zipEntry({ path: "/home/jon/wow!/notes.txt", archive: null })).toBeNull();
    expect(zipEntry({ path: "/home/jon/a.zip!/x.txt", archive: "/home/jon/b.zip" })).toBeNull();
  });
});

describe("downloadable", () => {
  it("keeps online-only files on the disk and leaves out zip entries", () => {
    const hits = [
      { path: "/d/a.txt", cloud: "online" as const, archive: null },
      { path: "/d/b.txt", cloud: "local" as const, archive: null },
      { path: "/d/z.zip!/c.txt", cloud: "online" as const, archive: "/d/z.zip" },
    ];
    expect(downloadable(hits).map((h) => h.path)).toEqual(["/d/a.txt"]);
  });
});

describe("baseName", () => {
  it("takes the last part of a path", () => {
    expect(baseName("/home/jon/backup.zip")).toBe("backup.zip");
    expect(baseName("C:\\Users\\jon\\backup.zip")).toBe("backup.zip");
  });
});
