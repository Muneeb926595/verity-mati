import { createSignal, For, onCleanup, onMount, Show } from "solid-js";
import { errorText, rpc } from "../api";
import type { EngineInfo } from "../types";
import { Banner, Spinner } from "./common";

const MODES = [
  {
    value: "auto",
    title: "Auto",
    text: "Uses the local AI model when it is available: to word answers, and to choose which of the places the analysis found best matches your question. It never adds facts — every statement is checked against the code.",
  },
  {
    value: "direct",
    title: "Facts only (Direct)",
    text: "No AI model. Answers are assembled straight from verified facts — the fastest and most literal option.",
  },
  {
    value: "fast",
    title: "Fast",
    text: "Uses a smaller, quicker AI model for the same jobs.",
  },
];

export default function Settings(props: { onClose: () => void }) {
  const [info, setInfo] = createSignal<EngineInfo | null>(null);
  const [mode, setMode] = createSignal<string>("");
  const [error, setError] = createSignal<string | null>(null);
  const [saving, setSaving] = createSignal(false);
  const [saved, setSaved] = createSignal(false);
  let dialog: HTMLDivElement | undefined;

  async function load() {
    try {
      const i = await rpc<EngineInfo>("engine.info");
      setInfo(i);
      setMode(i.settings?.llm_mode || i.llm?.mode || "auto");
    } catch (e) {
      setError(errorText(e));
    }
  }

  async function choose(m: string) {
    const before = mode();
    if (m === before || saving()) return;
    setMode(m);
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      await rpc("settings.set", { llm_mode: m });
      setSaved(true);
      load();
    } catch (e) {
      setMode(before);
      setError(errorText(e));
    } finally {
      setSaving(false);
    }
  }

  onMount(() => {
    load();
    const prev = document.activeElement as HTMLElement | null;
    dialog?.querySelector<HTMLElement>("input:checked, button")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") props.onClose();
    };
    document.addEventListener("keydown", onKey);
    onCleanup(() => {
      document.removeEventListener("keydown", onKey);
      prev?.focus?.();
    });
  });

  const chat = () => info()?.llm?.chat;
  const modelLine = () => {
    const i = info();
    if (!i) return "";
    if (mode() === "direct") return "Not used in Facts-only mode.";
    const c = chat();
    if (!c?.label) return "No AI model is available on this computer; answers use facts only.";
    const state = c.running ? `loaded${c.tok_per_s ? ` · ${Math.round(c.tok_per_s)} tokens/s` : ""}` : "starts with your first question";
    return `${c.label} — ${state}`;
  };

  return (
    <div class="modal-backdrop" onClick={(e) => e.target === e.currentTarget && props.onClose()}>
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="settings-h" ref={dialog}>
        <header class="modal-head">
          <h2 id="settings-h">Settings</h2>
          <button class="icon-btn" aria-label="Close settings" onClick={() => props.onClose()}>
            ×
          </button>
        </header>
        <div class="modal-body">
          <fieldset class="modes" disabled={!info() || saving()}>
            <legend class="field-label">How answers are worded</legend>
            <For each={MODES}>
              {(m) => (
                <label class="mode" classList={{ on: mode() === m.value }}>
                  <input type="radio" name="llm_mode" value={m.value} checked={mode() === m.value} onChange={() => choose(m.value)} />
                  <span class="mode-text">
                    <span class="mode-title">{m.title}</span>
                    <span class="mode-desc">{m.text}</span>
                  </span>
                </label>
              )}
            </For>
          </fieldset>
          <div class="save-state" aria-live="polite">
            <Show when={saving()}>
              <Spinner /> Saving…
            </Show>
            <Show when={saved() && !saving()}>
              <span class="ok">✓ Saved</span>
            </Show>
          </div>
          <Show when={error()}>
            <Banner kind="error">{error()}</Banner>
          </Show>

          <h3 class="field-label">AI model</h3>
          <p class="info-line">{info() ? modelLine() : <><Spinner /> Loading…</>}</p>

          <h3 class="field-label">This computer</h3>
          <Show when={info()} fallback={<p class="muted"><Spinner /> Loading…</p>}>
            <ul class="machine">
              <For each={info()!.machine || []}>{(l) => <li>{l}</li>}</For>
            </ul>
            <Show when={info()!.llm?.last_error}>
              <p class="muted small">Last AI model problem: {info()!.llm!.last_error}</p>
            </Show>
          </Show>
        </div>
        <footer class="modal-foot muted small">
          Verity {info()?.version || ""} · works fully offline — nothing leaves this computer.
        </footer>
      </div>
    </div>
  );
}
