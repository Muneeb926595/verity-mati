import { createMemo, createSignal, For, onCleanup, onMount, Show } from "solid-js";
import { errorText, rpc } from "../api";
import { isTauri } from "../tauri";
import type { Correction, GlossaryTerm, LearningListing, SourceTarget } from "../types";
import { dayText, plural } from "../util";
import { Banner, Inline, Spinner } from "./common";

type Tab = "corrections" | "glossary" | "share";
type Filter = "active" | "stale" | "removed" | "all";

const STATUS_LABEL: Record<string, string> = { active: "in use", stale: "stale", removed: "undone" };

/**
 * What Verity has learned for this project: corrections (in use, stale, undone), glossary terms and sharing.
 * Everything here is per project and stays on this computer unless it is exported.
 */
export default function LearningPanel(props: {
  path: string;
  name: string;
  onClose: () => void;
  onOpenSource: (t: SourceTarget) => void;
  onAsk: (question: string) => void;
  onChanged: () => void;
  initialTab?: Tab;
}) {
  const [tab, setTab] = createSignal<Tab>(props.initialTab || "corrections");
  const [data, setData] = createSignal<LearningListing | null>(null);
  const [filter, setFilter] = createSignal<Filter>("active");
  const [error, setError] = createSignal<string | null>(null);
  const [notice, setNotice] = createSignal<string | null>(null);
  const [busy, setBusy] = createSignal("");
  let dialog: HTMLDivElement | undefined;

  async function load() {
    try {
      const d = await rpc<LearningListing>("corrections.list", { path: props.path, status: "all" });
      setData(d);
      setError(null);
      if (filter() === "active" && !d.counts.active && d.counts.stale) setFilter("stale");
    } catch (e) {
      setError(errorText(e));
    }
  }

  /** Runs one change; true when it went through (a form is only cleared or closed then, so nothing typed is lost). */
  async function act(label: string, fn: () => Promise<{ message?: string } | unknown>): Promise<boolean> {
    if (busy()) return false;
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      const r = (await fn()) as { message?: string } | undefined;
      if (r && typeof r === "object" && "message" in r && r.message) setNotice(r.message);
      await load();
      props.onChanged();
      return true;
    } catch (e) {
      setError(errorText(e));
      return false;
    } finally {
      setBusy("");
    }
  }

  onMount(() => {
    load();
    const prev = document.activeElement as HTMLElement | null;
    dialog?.querySelector<HTMLElement>(".tab.on")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") props.onClose();
    };
    document.addEventListener("keydown", onKey);
    onCleanup(() => {
      document.removeEventListener("keydown", onKey);
      prev?.focus?.();
    });
  });

  const corrections = createMemo(() => {
    const d = data();
    if (!d) return [];
    return filter() === "all" ? d.corrections : d.corrections.filter((c) => c.status === filter());
  });
  const counts = () => data()?.counts || { active: 0, stale: 0, removed: 0, suggested: 0, accepted: 0, confirmations: 0 };

  return (
    <div class="modal-backdrop" onClick={(e) => e.target === e.currentTarget && props.onClose()}>
      <div class="modal wide learn-panel" role="dialog" aria-modal="true" aria-labelledby="learn-h" ref={dialog}>
        <header class="modal-head">
          <h2 id="learn-h">
            Learned for <span class="learn-proj">{props.name}</span>
          </h2>
          <button class="icon-btn" aria-label="Close" onClick={() => props.onClose()}>
            ×
          </button>
        </header>
        <div class="tabs" role="tablist" aria-label="What Verity learned">
          <TabButton id="corrections" label="Corrections" count={counts().active + counts().stale} />
          <TabButton id="glossary" label="Glossary" count={counts().suggested ? `${counts().suggested} new` : counts().accepted || null} />
          <TabButton id="share" label="Share" />
        </div>
        <div class="modal-body">
          <Show when={data()?.recovered_from}>
            <Banner kind="warn">
              The learning file of this project was damaged and has been started again. The damaged copy is kept at{" "}
              <code class="ic">{data()!.recovered_from}</code>.
            </Banner>
          </Show>
          <Show when={error()}>
            <Banner kind="error" onClose={() => setError(null)}>{error()}</Banner>
          </Show>
          <Show when={notice()}>
            <Banner kind="ok" onClose={() => setNotice(null)}>{notice()}</Banner>
          </Show>
          <Show when={data()} fallback={<p class="muted"><Spinner /> Loading…</p>}>
            <Show when={tab() === "corrections"}>{CorrectionsTab()}</Show>
            <Show when={tab() === "glossary"}>{GlossaryTab()}</Show>
            <Show when={tab() === "share"}>{ShareTab()}</Show>
          </Show>
        </div>
        <footer class="modal-foot muted small">
          Corrections only change which place an answer describes; every statement is still checked against the code.
          They stay on this computer unless you export them.
        </footer>
      </div>
    </div>
  );

  function TabButton(p: { id: Tab; label: string; count?: number | string | null }) {
    return (
      <button role="tab" class="tab" classList={{ on: tab() === p.id }} aria-selected={tab() === p.id} onClick={() => setTab(p.id)}>
        {p.label}
        <Show when={p.count}>
          <span class="count">{p.count}</span>
        </Show>
      </button>
    );
  }

  // ---------------------------------------------------------------------------------------- corrections
  function CorrectionsTab() {
    return (
      <div role="tabpanel" aria-label="Corrections">
        <Show when={(data()!.corrections.length || data()!.confirmations.length)} fallback={
          <div class="empty-learn">
            <p><strong>Nothing learned yet.</strong></p>
            <p class="muted">
              When an answer describes the wrong place, choose <em>This one</em> under “Other places that could be the answer”,
              or <em>pick a line</em> in the code. Verity uses it for this question and similar ones, says so in the answer,
              and keeps it here, where you can undo it.
            </p>
          </div>
        }>
          <div class="filters" role="group" aria-label="Show">
            <For each={[["active", "In use"], ["stale", "Stale"], ["removed", "Undone"], ["all", "All"]] as [Filter, string][]}>
              {([f, label]) => (
                <button class="chip" classList={{ on: filter() === f }} aria-pressed={filter() === f} onClick={() => setFilter(f)}>
                  {label}
                  <Show when={f !== "all"}> <span class="count">{(counts() as Record<string, number>)[f] || 0}</span></Show>
                </button>
              )}
            </For>
          </div>
          <Show when={filter() === "stale" && counts().stale}>
            <p class="muted small">
              The code these corrections point at changed or was removed, so they are never used. Restore or undo them, or ask the
              question again and pick the right place in the new code.
            </p>
          </Show>
          <ul class="corr-list">
            <For each={corrections()} fallback={<li class="muted">None.</li>}>{(c) => <CorrectionItem c={c} />}</For>
          </ul>
          <Show when={data()!.confirmations.length}>
            <h3 class="field-label">Confirmed answers <span class="count">{data()!.confirmations.length}</span></h3>
            <p class="muted small">Kept as test cases (Share → test suite). They never change answers.</p>
            <ul class="corr-list">
              <For each={data()!.confirmations}>
                {(k) => (
                  <li class="corr">
                    <div class="corr-head">
                      <span class="corr-id">✓</span>
                      <span class="corr-q">{k.question}</span>
                      <span class="muted small">{dayText(k.created)}</span>
                    </div>
                    <div class="corr-target muted small">must mention: {(k.must_mention || []).join(", ")}</div>
                    <div class="corr-actions">
                      <button class="link-btn" disabled={!!busy()} onClick={() => act("Removing…", () => rpc("answer.unconfirm", { path: props.path, id: k.id }))}>
                        Remove
                      </button>
                    </div>
                  </li>
                )}
              </For>
            </ul>
          </Show>
        </Show>
      </div>
    );
  }

  function CorrectionItem(p: { c: Correction }) {
    const c = p.c;
    const [editing, setEditing] = createSignal(false);
    const [noteText, setNoteText] = createSignal(c.note || "");
    return (
      <li class="corr" classList={{ stale: c.status === "stale", removed: c.status === "removed" }}>
        <div class="corr-head">
          <span class="corr-id">{c.id}</span>
          <span class={`corr-status st-${c.status}`}>{STATUS_LABEL[c.status] || c.status}</span>
          <span class="corr-q">{c.question}</span>
          <span class="muted small">{c.created_label || dayText(c.created)}</span>
        </div>
        <div class="corr-target">
          <span class="muted">Right place: </span>
          <button class="loc-inline" onClick={() => { props.onOpenSource({ file: c.target_file, line: c.target_line }); }}>
            {c.target_file}:{c.target_line}
          </button>{" "}
          <span class="corr-label"><Inline text={c.target_label.replace(/\s*\([^()]*:\d+\)\s*$/, "")} links={false} /></span>
        </div>
        <Show when={c.wrong?.label}>
          <div class="corr-wrong muted small">Instead of: <Inline text={c.wrong!.label!} links={false} /></div>
        </Show>
        <Show when={c.status !== "active" && c.status_reason}>
          <div class="corr-reason small" classList={{ "warn-text": c.status === "stale" }}>{c.status_reason}</div>
        </Show>
        <Show when={c.status === "active" && c.status_reason && /followed/.test(c.status_reason)}>
          <div class="corr-reason small muted">{c.status_reason}</div>
        </Show>
        <Show when={(c.conflict || []).length && c.status === "active"}>
          <div class="corr-reason small warn-text">Disagrees with {(c.conflict || []).join(", ")} on the same question; the newest is used.</div>
        </Show>
        <Show when={editing()} fallback={
          <Show when={c.note}>
            <div class="corr-note">“{c.note}”</div>
          </Show>
        }>
          <div class="corr-noteedit">
            <input type="text" maxLength={500} value={noteText()} aria-label="Note" onInput={(e) => setNoteText(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  act("Saving…", () => rpc("corrections.note", { path: props.path, id: c.id, note: noteText() })).then((ok) => ok && setEditing(false));
                }
              }} />
            <button class="btn sm primary" disabled={!!busy()} onClick={() => act("Saving…", () => rpc("corrections.note", { path: props.path, id: c.id, note: noteText() })).then((ok) => ok && setEditing(false))}>Save</button>
            <button class="btn sm" onClick={() => { setNoteText(c.note || ""); setEditing(false); }}>Cancel</button>
          </div>
        </Show>
        <div class="corr-actions">
          <Show when={c.uses}>
            <span class="muted small">used {plural(c.uses || 0, "time")}</span>
          </Show>
          <Show when={c.source === "import"}>
            <span class="muted small">imported</span>
          </Show>
          <button class="link-btn" onClick={() => { props.onAsk(c.question); props.onClose(); }}>Ask again</button>
          <button class="link-btn" onClick={() => setEditing(true)}>{c.note ? "Edit note" : "Add note"}</button>
          <Show when={c.status !== "removed"} fallback={
            <button class="link-btn" disabled={!!busy()} onClick={() => act("Restoring…", () => rpc("corrections.restore", { path: props.path, id: c.id }))}>Restore</button>
          }>
            <button class="link-btn danger" disabled={!!busy()} onClick={() => act("Undoing…", () => rpc("corrections.remove", { path: props.path, id: c.id }))}>Undo</button>
          </Show>
        </div>
      </li>
    );
  }

  // ---------------------------------------------------------------------------------------- glossary
  function GlossaryTab() {
    const [term, setTerm] = createSignal("");
    const [idents, setIdents] = createSignal("");
    const terms = () => data()!.glossary;
    const group = (s: string) => terms().filter((t) => t.status === s);
    return (
      <div role="tabpanel" aria-label="Glossary">
        <p class="muted small">
          Words people use that the code spells differently (“bus-off” → <code class="ic">CO_CAN_ERRTX_BUS_OFF</code>). An accepted
          term is used to find code for every question on this project. Suggestions come from your corrections and do nothing
          until you accept them.
        </p>
        <Show when={group("suggested").length}>
          <h3 class="field-label">Suggested <span class="count">{group("suggested").length}</span></h3>
          <ul class="terms"><For each={group("suggested")}>{(t) => <TermItem t={t} />}</For></ul>
        </Show>
        <h3 class="field-label">In use <span class="count">{group("accepted").length}</span></h3>
        <ul class="terms">
          <For each={group("accepted")} fallback={<li class="muted">No terms yet.</li>}>{(t) => <TermItem t={t} />}</For>
        </ul>
        <form class="term-add" onSubmit={(e) => {
          e.preventDefault();
          if (!term().trim() || !idents().trim()) return;
          act("Adding…", () => rpc("glossary.add", { path: props.path, term: term(), identifiers: idents() })).then((ok) => { if (ok) { setTerm(""); setIdents(""); } });
        }}>
          <h3 class="field-label">Add a term</h3>
          <div class="term-add-row">
            <input type="text" placeholder="word or phrase, e.g. bus-off" aria-label="Term" maxLength={48} value={term()} onInput={(e) => setTerm(e.currentTarget.value)} />
            <span aria-hidden="true">→</span>
            <input type="text" placeholder="identifiers, e.g. CO_CAN_ERRTX_BUS_OFF" aria-label="Identifiers (comma separated)" value={idents()} onInput={(e) => setIdents(e.currentTarget.value)} />
            <button class="btn sm primary" type="submit" disabled={!!busy() || !term().trim() || !idents().trim()}>Add</button>
          </div>
        </form>
        <Show when={group("rejected").length}>
          <details class="rejected">
            <summary class="muted small">{plural(group("rejected").length, "rejected suggestion")}</summary>
            <ul class="terms"><For each={group("rejected")}>{(t) => <TermItem t={t} />}</For></ul>
          </details>
        </Show>
      </div>
    );
  }

  function TermItem(p: { t: GlossaryTerm }) {
    const t = p.t;
    return (
      <li class="term">
        <span class="term-w">{t.term}</span>
        <span aria-hidden="true" class="muted">→</span>
        <span class="term-ids">
          <For each={t.identifiers}>{(i) => <code class="ic">{i}</code>}</For>
        </span>
        <span class="term-src muted small">{t.read_only ? "from verity.toml" : t.source?.startsWith("c-") ? `from ${t.source}` : t.source}</span>
        <span class="term-actions">
          <Show when={t.status === "suggested"}>
            <button class="btn sm primary" disabled={!!busy()} onClick={() => act("Accepting…", () => rpc("glossary.accept", { path: props.path, term: t.term }))}>Accept</button>
            <button class="btn sm" disabled={!!busy()} onClick={() => act("Rejecting…", () => rpc("glossary.reject", { path: props.path, term: t.term }))}>Reject</button>
          </Show>
          <Show when={t.status === "accepted" && !t.read_only}>
            <button class="link-btn danger" disabled={!!busy()} onClick={() => act("Removing…", () => rpc("glossary.remove", { path: props.path, term: t.term }))}>Remove</button>
          </Show>
          <Show when={t.status === "rejected"}>
            <button class="link-btn" disabled={!!busy()} onClick={() => act("Accepting…", () => rpc("glossary.accept", { path: props.path, term: t.term }))}>Accept after all</button>
          </Show>
        </span>
      </li>
    );
  }

  // ---------------------------------------------------------------------------------------- share
  function ShareTab() {
    const [toml, setToml] = createSignal<string | null>(null);
    const [copied, setCopied] = createSignal(false);
    let fileInput: HTMLInputElement | undefined;

    async function download(format: "json" | "bench") {
      await act("Exporting…", async () => {
        if (isTauri()) {
          // the desktop app's web view cannot save downloads: the engine writes the file into Downloads
          const r = await rpc<{ saved?: string }>("corrections.export", { path: props.path, format, save: true });
          return { message: `Saved ${r.saved}` };
        }
        const r = await rpc<{ filename: string; data: unknown }>("corrections.export", { path: props.path, format });
        const blob = new Blob([JSON.stringify(r.data, null, 1)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const el = document.createElement("a");
        el.href = url;
        el.download = r.filename;
        document.body.appendChild(el);
        el.click();
        el.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        return { message: `Downloaded ${r.filename}` };
      });
    }

    async function showToml() {
      try {
        const r = await rpc<{ data: string }>("corrections.export", { path: props.path, format: "toml" });
        setToml(r.data);
      } catch (e) {
        setError(errorText(e));
      }
    }

    async function importFile(f: File) {
      const text = await f.text();
      await act("Importing…", () => rpc("corrections.import", { path: props.path, data: text }));
    }

    return (
      <div role="tabpanel" aria-label="Share" class="share">
        <section class="share-item">
          <h3 class="field-label">Share with your team</h3>
          <p class="muted small">
            A file with this project's corrections and glossary. Someone else imports it on their computer; corrections whose
            code is different there turn stale instead of being used.
          </p>
          <div class="share-row">
            <button class="btn sm" disabled={!!busy()} onClick={() => download("json")}>Export corrections…</button>
            <button class="btn sm" disabled={!!busy()} onClick={() => fileInput?.click()}>Import a file…</button>
            <input ref={fileInput} type="file" accept=".json,application/json" class="sr-only" aria-label="Import a learning file"
              onChange={(e) => { const f = e.currentTarget.files?.[0]; e.currentTarget.value = ""; if (f) importFile(f); }} />
          </div>
        </section>
        <section class="share-item">
          <h3 class="field-label">Test suite</h3>
          <p class="muted small">
            Every correction and confirmed answer as a known-answer test: the question, and what a right answer must mention.
            Run it after upgrading Verity or changing the code: <code class="ic">python -m verity.bench.run --suite FILE</code>.
          </p>
          <div class="share-row">
            <button class="btn sm" disabled={!!busy() || !(counts().active + counts().confirmations)} onClick={() => download("bench")}>Export test suite…</button>
          </div>
        </section>
        <section class="share-item">
          <h3 class="field-label">Glossary for the repository</h3>
          <p class="muted small">Commit accepted terms to the repository's <code class="ic">verity.toml</code> and everyone who indexes it gets them.</p>
          <div class="share-row">
            <button class="btn sm" disabled={!counts().accepted} onClick={showToml}>Show verity.toml lines</button>
          </div>
          <Show when={toml()}>
            <pre class="toml">{toml()}</pre>
            <button class="btn sm" onClick={async () => {
              try {
                await navigator.clipboard.writeText(toml() || "");
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              } catch {
                setError("Copying is not allowed here; select the text and copy it.");
              }
            }}>{copied() ? "✓ Copied" : "Copy"}</button>
          </Show>
        </section>
      </div>
    );
  }
}
