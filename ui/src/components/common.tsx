import { createContext, createSignal, For, type JSX, Show, useContext } from "solid-js";
import type { SourceTarget } from "../types";
import { certainty, loc, parseInline } from "../util";

// ------------------------------------------------------------------------------------------ source viewer context
export const SourceCtx = createContext<{ open: (t: SourceTarget) => void }>({ open: () => {} });
export const useSource = () => useContext(SourceCtx);

// ------------------------------------------------------------------------------------------ small pieces
export function Spinner(props: { size?: "sm" | "md" | "lg"; label?: string }) {
  return <span class={`spinner ${props.size || "sm"}`} role={props.label ? "status" : undefined} aria-label={props.label} />;
}

export function Banner(props: {
  kind: "error" | "warn" | "info" | "ok";
  children: JSX.Element;
  onClose?: () => void;
  details?: string | null;
  action?: JSX.Element;
}) {
  const [showDetails, setShowDetails] = createSignal(false);
  return (
    <div class={`banner ${props.kind}`} role={props.kind === "error" ? "alert" : "status"}>
      <div class="banner-row">
        <span class="banner-icon" aria-hidden="true">
          {props.kind === "error" ? "!" : props.kind === "warn" ? "!" : props.kind === "ok" ? "✓" : "i"}
        </span>
        <div class="banner-text">{props.children}</div>
        <Show when={props.action}>{props.action}</Show>
        <Show when={props.details}>
          <button class="link-btn" onClick={() => setShowDetails(!showDetails())}>
            {showDetails() ? "Hide details" : "Details"}
          </button>
        </Show>
        <Show when={props.onClose}>
          <button class="icon-btn" aria-label="Dismiss" onClick={() => props.onClose?.()}>
            ×
          </button>
        </Show>
      </div>
      <Show when={showDetails() && props.details}>
        <pre class="banner-details">{props.details}</pre>
      </Show>
    </div>
  );
}

/** Engine prose: `code` spans become inline code; file.c:12 references become links to the source viewer. */
export function Inline(props: { text: string | null | undefined; links?: boolean }) {
  const src = useSource();
  const pieces = () => parseInline(props.text || "", props.links !== false);
  return (
    <For each={pieces()}>
      {(p) =>
        p.t === "code" ? (
          <code class="ic" classList={{ nw: p.v.length <= 40 }}>
            {p.v.length > 40 ? p.v.replace(/->/g, "-\u2060>") : p.v}
          </code>
        ) : p.t === "loc" ? (
          <button class="loc-inline" onClick={() => src.open({ file: p.file, line: p.line })} title="Show this line">
            {p.v}
          </button>
        ) : (
          p.v
        )
      }
    </For>
  );
}

export function LocLink(props: { file?: string | null; line?: number | null; end?: number | null; class?: string }) {
  const src = useSource();
  return (
    <Show when={props.file}>
      <button
        class={`loc ${props.class || ""}`}
        onClick={(e) => {
          e.stopPropagation();
          src.open({ file: props.file!, line: props.line, end: props.end });
        }}
        title="Open this place in the code"
      >
        {loc(props.file, props.line, props.end)}
      </button>
    </Show>
  );
}

export function CertaintyBadge(props: { status?: string; label?: string; compact?: boolean }) {
  const c = () => certainty(props.status, props.label);
  return (
    <span class={`cert ${c().cls}`} title={c().label}>
      <span class="dot" aria-hidden="true" />
      <Show when={!props.compact} fallback={<span class="sr-only">{c().label}</span>}>
        {c().label}
      </Show>
    </span>
  );
}

let collId = 0;
export function Collapsible(props: { title: string; count?: number | string | null; children: JSX.Element; hint?: string }) {
  const [open, setOpen] = createSignal(false);
  const id = `coll-${++collId}`;
  return (
    <section class="collapsible" classList={{ open: open() }}>
      <h3 class="coll-h">
        <button class="coll-head" aria-expanded={open()} aria-controls={id} onClick={() => setOpen(!open())}>
          <span class="chev" aria-hidden="true" />
          <span class="coll-title">{props.title}</span>
          <Show when={props.count !== undefined && props.count !== null && props.count !== ""}>
            <span class="count">{props.count}</span>
          </Show>
          <Show when={props.hint}>
            <span class="coll-hint">{props.hint}</span>
          </Show>
        </button>
      </h3>
      <Show when={open()}>
        <div class="coll-body" id={id}>
          {props.children}
        </div>
      </Show>
    </section>
  );
}

/** Renders the first `step` items and a "Show more" button (keeps long lists cheap). */
export function Limited<T>(props: {
  each: T[];
  step?: number;
  children: (item: T, index: () => number) => JSX.Element;
  wrap?: (children: JSX.Element) => JSX.Element;
  noun?: string;
}) {
  const step = props.step ?? 50;
  const [limit, setLimit] = createSignal(step);
  const shown = () => props.each.slice(0, limit());
  const rest = () => props.each.length - shown().length;
  const list = <For each={shown()}>{(it, i) => props.children(it, i)}</For>;
  return (
    <>
      {props.wrap ? props.wrap(list) : list}
      <Show when={rest() > 0}>
        <button class="btn subtle more-btn" onClick={() => setLimit(limit() + step)}>
          Show more ({rest()} more {props.noun || "items"})
        </button>
      </Show>
    </>
  );
}
