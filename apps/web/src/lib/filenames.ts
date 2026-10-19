/**
 * Reduce a peer-supplied filename to a plain name — same rules as
 * packages/transfer/filenames.py. The browser sanitizes download names
 * too, but we also display this name, so clean it ourselves.
 */

// Path separators, characters Windows forbids, and control characters.
const UNSAFE = /[\\/<>:"|?*\x00-\x1f\x7f]/g;
const MAX_NAME_BYTES = 255;
const FALLBACK = "download";

export function safeFilename(name: string): string {
  let clean = name.split(/[\\/]/).pop() ?? "";
  clean = clean.replace(UNSAFE, "_").trim().replace(/^\.+|\.+$/g, "");
  if (!clean) return FALLBACK;

  const encoder = new TextEncoder();
  if (encoder.encode(clean).length <= MAX_NAME_BYTES) return clean;

  const dot = clean.lastIndexOf(".");
  const suffix = dot > 0 && clean.length - dot < 32 ? clean.slice(dot) : "";
  let stem = suffix ? clean.slice(0, dot) : clean;
  const budget = MAX_NAME_BYTES - encoder.encode(suffix).length;
  while (encoder.encode(stem).length > budget) stem = stem.slice(0, -1);
  return stem + suffix;
}
