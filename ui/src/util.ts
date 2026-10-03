import type { Certainty } from "./types";

/** ["c"] → "C", ["csharp"] → "C#", both → "C and C#". Empty → "". */
export function languagesLabel(langs: string[] | null | undefined): string {
  if (!langs || !langs.length) return "";
  const names: string[] = [];
  for (const l of langs) {
    const k = l.toLowerCase();
    const n = k === "c" ? "C" : k === "csharp" || k === "cs" || k === "c#" ? "C#" : k === "cpp" || k === "c++" ? "C++" : l;
    if (!names.includes(n)) names.push(n);
  }
  names.sort((a, b) => (a === "C" ? -1 : b === "C" ? 1 : a.localeCompare(b)));
  if (names.length === 1) return names[0];
  return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}

export function plural(n: number, one: string, many = one + "s"): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function etaText(s: number | null | undefined): string {
  if (s == null || !isFinite(s)) return "";
  if (s < 1) return "almost done";
  if (s < 60) return `about ${Math.ceil(s)} s left`;
  const m = Math.round(s / 60);
  return `about ${m} min left`;
}

export function timeAgo(ts: number | null | undefined): string {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  if (d < 14) return `${d} day${d === 1 ? "" : "s"} ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function seconds(s: number | null | undefined): string {
  if (s == null) return "";
  if (s < 0.1) return "under 0.1 s";
  if (s < 10) return `${s.toFixed(1)} s`;
  return `${Math.round(s)} s`;
}

// ------------------------------------------------------------------------------------------ certainty
export interface CertaintyInfo {
  key: string;
  label: string;
  short: string;
  cls: string;
}

const CERTAINTY: Record<string, CertaintyInfo> = {
  CONFIRMED: { key: "CONFIRMED", label: "Confirmed in the code", short: "confirmed", cls: "c-confirmed" },
  OBSERVED: { key: "OBSERVED", label: "Observed by running the code", short: "observed", cls: "c-observed" },
  INFERENCE: { key: "INFERENCE", label: "Inferred (likely, not certain)", short: "inferred", cls: "c-inference" },
  "RUNTIME-DEPENDENT": { key: "RUNTIME-DEPENDENT", label: "Depends on runtime values", short: "depends on runtime values", cls: "c-runtime" },
  UNKNOWN: { key: "UNKNOWN", label: "Could not be determined", short: "undetermined", cls: "c-unknown" },
};

export function certainty(status: Certainty | undefined, label?: string): CertaintyInfo {
  const c = CERTAINTY[String(status || "UNKNOWN").toUpperCase()] || CERTAINTY.UNKNOWN;
  return label ? { ...c, label } : c;
}

export const CERTAINTY_ORDER = ["CONFIRMED", "OBSERVED", "INFERENCE", "RUNTIME-DEPENDENT", "UNKNOWN"];

// ------------------------------------------------------------------------------------------ inline text
export type Piece =
  | { t: "text"; v: string }
  | { t: "code"; v: string }
  | { t: "loc"; v: string; file: string; line: number };

// file.ext:123 references inside plain prose (never inside `code`)
const LOC_RE = /([A-Za-z0-9_.\-/]+\.(?:c|h|cs|cc|cpp|cxx|hpp|hh|inc)):(\d+)/g;

/** Splits engine prose into text, `inline code` and clickable file:line pieces. */
export function parseInline(s: string, links = true): Piece[] {
  const out: Piece[] = [];
  const parts = String(s ?? "").split("`");
  // an odd number of backticks leaves the last piece unpaired: treat it as text
  const paired = parts.length % 2 === 1 ? parts.length : parts.length - 1;
  parts.forEach((p, i) => {
    if (!p) return;
    const isCode = i % 2 === 1 && i < paired;
    if (isCode) {
      out.push({ t: "code", v: p });
      return;
    }
    const text = i % 2 === 1 ? "`" + p : p;
    if (!links) {
      out.push({ t: "text", v: text });
      return;
    }
    let last = 0;
    LOC_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = LOC_RE.exec(text))) {
      if (m.index > last) out.push({ t: "text", v: text.slice(last, m.index) });
      out.push({ t: "loc", v: m[0], file: m[1], line: Number(m[2]) });
      last = m.index + m[0].length;
    }
    if (last < text.length) out.push({ t: "text", v: text.slice(last) });
  });
  return out;
}

export function loc(file?: string | null, line?: number | null, end?: number | null): string {
  if (!file) return "";
  if (!line) return file;
  if (end && end !== line) return `${file}:${line}–${end}`;
  return `${file}:${line}`;
}

export function shortPath(p: string): string {
  const home = /^\/Users\/[^/]+/;
  return p.replace(home, "~");
}

export function valueText(v: unknown): string {
  if (v === null || v === undefined) return "–";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** 2 Oct 2026 */
export function dayText(ts: number | null | undefined): string {
  if (!ts) return "";
  // day month year everywhere (the engine writes "2 Oct 2026" too)
  return new Date(ts * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}
