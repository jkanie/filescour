import { describe, expect, it } from "vitest";

import {
  FormError,
  emptyForm,
  emptyLimits,
  formatSize,
  limitParams,
  mbToBytes,
  pageLabel,
  pages,
  splitExts,
  toParams,
} from "./query";

const form = (over: Partial<typeof emptyForm>) => ({ ...emptyForm, ...over });

describe("toParams", () => {
  it("sends only the query, mode and case for an empty form", () => {
    expect(toParams(emptyForm).toString()).toBe("q=&mode=name&ignore_case=true");
  });

  it("labels a page and finds the pages around it", () => {
    expect(pageLabel(0, 3, 3)).toBe("3 files");
    expect(pageLabel(0, 1, 1)).toBe("1 file");
    expect(pageLabel(0, 200, 950)).toBe("Showing 1-200 of 950 files");
    expect(pageLabel(800, 150, 950)).toBe("Showing 801-950 of 950 files");
    expect(pages(0, 200, 950)).toEqual({ previous: null, next: 200 });
    expect(pages(200, 200, 950)).toEqual({ previous: 0, next: 400 });
    expect(pages(800, 150, 950)).toEqual({ previous: 600, next: null });
    expect(pages(0, 3, 3)).toEqual({ previous: null, next: null });
    // A start that is not a multiple of the page size still goes back to 0.
    expect(pages(150, 200, 950).previous).toBe(0);
  });

  it("sends a sort only when it is not the API's default", () => {
    expect(toParams(form({ sort: "path" })).has("sort")).toBe(false);
    expect(toParams(form({ sort: "-size" })).toString()).toBe(
      "q=&mode=name&ignore_case=true&sort=-size",
    );
    expect(toParams(form({ sort: "modified" })).get("sort")).toBe("modified");
  });

  it("sends every filter in the names the API reads", () => {
    const p = toParams(
      form({
        q: "^total",
        mode: "regex",
        ignoreCase: false,
        folder: "  /home/jon/docs ",
        ext: "pdf, .MD,pdf,,",
        minSizeMb: "0.5",
        maxSizeMb: "2",
        modifiedFrom: "2026-01-31",
        modifiedTo: "2026-02-28",
        source: "pdf",
        cloud: "online",
      }),
    );
    expect([...p.entries()]).toEqual([
      ["q", "^total"],
      ["mode", "regex"],
      ["ignore_case", "false"],
      ["folder", "/home/jon/docs"],
      ["ext", "pdf"],
      ["ext", "md"],
      ["min_size", "524288"],
      ["max_size", "2097152"],
      ["modified_after", "2026-01-31T00:00:00.000Z"],
      ["modified_before", "2026-03-01T00:00:00.000Z"],
      ["source", "pdf"],
      ["cloud", "online"],
    ]);
  });

  it("includes the whole last day, across a year end", () => {
    const p = toParams(form({ modifiedTo: "2025-12-31" }));
    expect(p.get("modified_before")).toBe("2026-01-01T00:00:00.000Z");
  });

  it.each([
    [{ minSizeMb: "-1" }, "Smallest size must be"],
    [{ maxSizeMb: "lots" }, "Largest size must be"],
    [{ minSizeMb: "3", maxSizeMb: "2" }, "larger than largest"],
    [{ modifiedFrom: "2026-02-30" }, "Modified from must be a date"],
    [{ modifiedTo: "31/01/2026" }, "Modified to must be a date"],
    [{ mode: "regex" as const, q: " " }, "regular expression"],
    [{ mode: "words" as const, q: "" }, "at least 1 word"],
  ])("refuses %o", (over, msg) => {
    expect(() => toParams(form(over))).toThrow(FormError);
    expect(() => toParams(form(over))).toThrow(msg);
  });
});

describe("helpers", () => {
  it("converts MB to bytes", () => {
    expect(mbToBytes("", "x")).toBeNull();
    expect(mbToBytes("1", "x")).toBe(1048576);
    expect(mbToBytes("0", "x")).toBe(0);
  });

  it("splits extensions", () => {
    expect(splitExts(" .Txt , md,TXT ")).toEqual(["txt", "md"]);
  });

  it("formats sizes", () => {
    expect(formatSize(0)).toBe("0 B");
    expect(formatSize(1023)).toBe("1023 B");
    expect(formatSize(1536)).toBe("1.5 KB");
    expect(formatSize(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatSize(300 * 1024 * 1024 * 1024)).toBe("300 GB");
  });
});

describe("limitParams", () => {
  it("sends only the limits that were typed", () => {
    const p = limitParams({ freeFloorMb: " 4096 ", maxFileMb: "", waitSeconds: "030" });
    expect([...p.entries()]).toEqual([
      ["free_floor_mb", "4096"],
      ["wait_seconds", "30"],
    ]);
    expect([...limitParams(emptyLimits).entries()]).toEqual([]);
  });

  it("refuses anything but a whole number", () => {
    for (const bad of ["-1", "1.5", "2 GB"]) {
      expect(() => limitParams({ ...emptyLimits, maxFileMb: bad })).toThrow(
        "Largest file must be a whole number, 0 or more.",
      );
    }
  });
});
