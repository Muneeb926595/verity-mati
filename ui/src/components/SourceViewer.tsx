import { createMemo, createSignal, For, onCleanup, onMount, Show } from "solid-js";
import { errorText, rpc } from "../api";
import { highlight } from "../highlight";
import type { SourceSlice, SourceTarget } from "../types";
import { Banner, Spinner } from "./common";

const MAX_LINES = 800; // never render more than this many lines at once

export default function SourceViewer(props: { path: string; target: SourceTarget; onClose: () => void }) {
  const [slice, setSlice] = createSignal<SourceSlice | null>(null);
  const [error, setError] = createSignal<string | null>(null);
  const [loading, setLoading] = createSignal(false);
  let scroller: HTMLDivElement | undefined;
  let panel: HTMLElement | undefined;
  let closeBtn: HTMLButtonElement | undefined;
  const line = () => props.target.line || 1;
  const end = () => Math.max(line(), props.target.end || line());

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const s = await rpc<SourceSlice>("source.get", { path: props.path, file: props.target.file, around: line() });
      setSlice(s);
      requestAnimationFrame(() => {
        const el = scroller?.querySelector<HTMLElement>(".row.target");
        el?.scrollIntoView({ block: "center" });
      });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }

  async function more(direction: "up" | "down") {
    const s = slice();
    if (!s || loading()) return;
    const start = direction === "up" ? Math.max(1, s.start - 80) : s.end + 1;
    const stop = direction === "up" ? s.start - 1 : Math.min(s.total, s.end + 80);
    if (stop < start) return;
    setLoading(true);
    const keep = scroller ? scroller.scrollHeight - scroller.scrollTop : 0;
    try {
      const add = await rpc<SourceSlice>("source.get", { path: props.path, file: s.file, start, end: stop });
      let lines = direction === "up" ? [...add.lines, ...s.lines] : [...s.lines, ...add.lines];
      let nStart = direction === "up" ? add.start : s.start;
      if (lines.length > MAX_LINES) {
        // drop lines from the far side so the page stays light
        const drop = lines.length - MAX_LINES;
        if (direction === "up") lines = lines.slice(0, MAX_LINES);
        else {
          lines = lines.slice(drop);
          nStart += drop;
        }
      }
      setSlice({ ...s, start: nStart, end: nStart + lines.length - 1, lines, stale: s.stale || add.stale });
      if (direction === "up" && scroller) {
        const el = scroller;
        requestAnimationFrame(() => (el.scrollTop = el.scrollHeight - keep));
      }
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }

  const rows = createMemo(() => {
    const s = slice();
    if (!s) return [];
    const toks = highlight(s.lines, s.lang);
    return s.lines.map((_, i) => ({ n: s.start + i, toks: toks[i] }));
  });

  onMount(() => {
    load();
    const prev = document.activeElement as HTMLElement | null;
    closeBtn?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        props.onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    onCleanup(() => {
      document.removeEventListener("keydown", onKey);
      prev?.focus?.({ preventScroll: true });
    });
  });

  return (
    <>
      <div class="panel-backdrop" onClick={() => props.onClose()} />
      <aside class="source-panel" ref={panel} role="dialog" aria-label={`Source code: ${props.target.file}`}>
        <header class="src-head">
          <div class="src-title">
            <span class="src-file" title={props.target.file}>
              {props.target.file}
            </span>
            <span class="src-line">
              {end() > line() ? `lines ${line()}–${end()}` : `line ${line()}`}
              <Show when={slice()}> of {slice()!.total}</Show>
            </span>
          </div>
          <Show when={loading()}>
            <Spinner />
          </Show>
          <span class="src-ro" title="Verity never changes your files">read-only</span>
          <button class="icon-btn" ref={closeBtn} aria-label="Close source viewer" title="Close (Esc)" onClick={() => props.onClose()}>
            ×
          </button>
        </header>
        <Show when={slice()?.stale}>
          <div class="src-banner">
            This file has changed since Verity last read the project, so the highlighted line may have moved. Press Update at the
            top to refresh.
          </div>
        </Show>
        <Show when={error()}>
          <div class="src-error">
            <Banner kind="error">{error()}</Banner>
          </div>
        </Show>
        <div class="src-body" ref={scroller}>
          <Show when={slice() && slice()!.start > 1}>
            <button class="src-more" onClick={() => more("up")} disabled={loading()}>
              Show earlier lines
            </button>
          </Show>
          <div class="code" role="list">
            <For each={rows()}>
              {(r) => (
                <div
                  class="row"
                  role="listitem"
                  classList={{ hl: r.n >= line() && r.n <= end(), target: r.n === line() }}
                >
                  <span class="ln" aria-hidden="true">
                    {r.n}
                  </span>
                  <span class="tx">
                    <For each={r.toks}>{(t) => (t.k ? <span class={`t-${t.k}`}>{t.v}</span> : t.v)}</For>
                    {r.toks.length ? "" : " "}
                  </span>
                </div>
              )}
            </For>
          </div>
          <Show when={slice() && slice()!.end < slice()!.total}>
            <button class="src-more" onClick={() => more("down")} disabled={loading()}>
              Show later lines
            </button>
          </Show>
          <Show when={!slice() && loading()}>
            <div class="muted pad">
              <Spinner /> Loading…
            </div>
          </Show>
        </div>
      </aside>
    </>
  );
}
