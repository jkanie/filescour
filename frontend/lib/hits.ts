// Rules for showing hits, kept free of React such that they can be tested.

import type { Hit } from "./api";

// An entry of a zip file has a path such as /home/jon/backup.zip!/notes/may.txt,
// and the API names its zip in `archive`. A folder whose own name ends in "!"
// gives a path of the same form, so only `archive` tells the 2 apart.
export function zipEntry(
  hit: Pick<Hit, "path" | "archive">,
): { zip: string; entry: string } | null {
  if (!hit.archive) return null;
  const head = `${hit.archive}!/`;
  if (!hit.path.startsWith(head)) return null;
  return { zip: hit.archive, entry: hit.path.slice(head.length) };
}

// The online-only files that a down-load can bring to this disk. A zip entry
// is not a file on the disk, so it is never down-loaded on its own.
export function downloadable<T extends Pick<Hit, "cloud" | "archive">>(hits: T[]): T[] {
  return hits.filter((h) => h.cloud === "online" && !h.archive);
}

export function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}
