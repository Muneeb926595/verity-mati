// Shapes of the engine's JSON (docs/ENGINE_API.md, ui/fixtures/*.json). Only the fields the UI reads are typed.

export type Certainty = "CONFIRMED" | "OBSERVED" | "INFERENCE" | "RUNTIME-DEPENDENT" | "UNKNOWN" | (string & {});

export type JobState = "queued" | "running" | "done" | "failed" | "cancelled";

export interface Job {
  id: string;
  kind: "index" | "ask" | (string & {});
  project: string;
  state: JobState;
  created?: number;
  started?: number | null;
  finished?: number | null;
  progress?: any;
  error?: string | null;
  params?: Record<string, unknown>;
  result?: any;
}

export interface PhaseSummary {
  state?: string;
  detail?: string;
  seconds?: number;
}

export interface ProjectStatus {
  path: string;
  name: string;
  id: string;
  state: "new" | "running" | "complete" | "cancelled" | "failed" | (string & {});
  label: string;
  ready: boolean | number | null;
  languages: string[];
  counts?: Record<string, number>;
  stale: string[];
  indexed_at?: number | null;
  summary?: { phases?: Record<string, PhaseSummary>; warnings?: string[]; state?: string; seconds?: number };
  jobs: Job[];
}

export interface RecentProject {
  path: string;
  name: string;
  opened?: number;
  exists: boolean;
  label?: string;
  ready?: boolean | number | null;
  languages?: string[];
}

export interface IndexProgressEvent {
  type: "index.progress";
  job: string;
  project: string;
  phase: string;
  label: string;
  phase_index: number;
  phase_total: number;
  state: "running" | "done" | "skipped" | "failed";
  detail?: string;
  done?: number | null;
  total?: number | null;
  eta_s?: number | null;
  file?: string;
  elapsed_s?: number;
  languages?: string[];
}

export interface AskProgressEvent {
  type: "ask.progress";
  job: string;
  project: string;
  step: number;
  steps: number;
  label: string;
  detail?: string;
  elapsed_s?: number;
}

export interface FlowStep {
  label: string;
  file?: string | null;
  line?: number | null;
  detail?: string;
}

export interface Evidence {
  id: string;
  kind?: string;
  file?: string | null;
  line?: number | null;
  end_line?: number | null;
  text?: string;
  note?: string;
  status: Certainty;
  status_label?: string;
  sym?: string;
  also?: string[];
}

export interface Claim {
  text: string;
  status: Certainty;
  status_label?: string;
  why?: string;
  verified?: boolean;
  check?: string;
  ev?: string[];
}

export interface PathStep {
  label: string;
  file?: string | null;
  line?: number | null;
  status?: Certainty;
  note?: string;
  ev?: string;
}

export interface RelatedItem {
  key?: string;
  label: string;
  file?: string | null;
  line?: number | null;
  end_line?: number | null;
}

export interface Verification {
  report?: {
    claims?: number;
    verified?: number;
    rejected?: number;
    evidence?: number;
    evidence_ok?: number;
    stale_files?: string[];
  };
  unknowns?: string[];
  notes?: string[];
  rejected?: Array<{ text?: string } | string>;
}

export interface Run {
  label?: string;
  inputs?: Record<string, unknown>;
  calls?: string[];
  hw_calls?: string[];
  state?: Record<string, unknown>;
  seconds?: number;
  ok?: boolean;
  exit?: number;
  error?: string;
}

export interface Answer {
  id?: string;
  question: string;
  answer: string[];
  flow?: FlowStep[];
  evidence?: Evidence[];
  status?: Certainty;
  status_label?: string;
  status_counts?: Record<string, number>;
  sections?: {
    reasoning?: Claim[];
    execution_path?: PathStep[];
    related?: RelatedItem[];
    assumptions?: string[];
    verification?: Verification;
  };
  run?: { ok?: boolean; runs?: Run[]; explain?: string; error?: string } | null;
  meta?: { seconds?: number; prose?: { mode?: string; model?: string; fallback?: string | boolean; skipped?: string }; intent?: string; model?: string | null; model_choices?: number };
}

export interface SourceSlice {
  file: string;
  start: number;
  end: number;
  total: number;
  lines: string[];
  stale: boolean;
  lang: string;
}

export interface HistoryItem {
  id: string;
  question: string;
  created: number;
}

export interface EngineInfo {
  version?: string;
  machine?: string[];
  llm?: {
    mode?: string;
    chat?: { label?: string | null; running?: boolean; tok_per_s?: number | null };
    embed?: { label?: string | null; running?: boolean };
    last_error?: string | null;
  };
  settings?: { llm_mode?: string; embeddings?: boolean };
  offline?: boolean;
}

/** Where the source viewer should point. */
export interface SourceTarget {
  file: string;
  line?: number | null;
  end?: number | null;
}
