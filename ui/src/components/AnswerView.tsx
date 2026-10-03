import { createMemo, For, Show } from "solid-js";
import type { Answer, Evidence, LearningUse, Run } from "../types";
import { certainty, CERTAINTY_ORDER, dayText, loc, plural, seconds, valueText } from "../util";
import { CertaintyBadge, Collapsible, Inline, Limited, LocLink, Spinner, useSource } from "./common";

/** What the person can do with an answer to teach Verity (handled by the project view). */
export interface LearnHandlers {
  /** "This one": alternative `rank` is the right place */
  pick: (rank: number) => void;
  /** "The answer was right" (kept as a test case) */
  confirm: () => void;
  /** "None of these": open the source viewer to pick the line */
  pickLine: () => void;
  /** undo a correction this answer used */
  undo: (id: string) => void;
  /** what is being saved right now ("" = nothing), and the last result for this answer */
  busy: () => string;
  done: () => { kind: "corrected" | "confirmed"; text: string } | null;
}

function sameText(a: string, b: string): boolean {
  const norm = (s: string) => s.replace(/[`;\s]/g, "");
  const x = norm(a);
  const y = norm(b);
  return !!x && (x === y || y.includes(x));
}

export default function AnswerView(props: { answer: Answer; learn?: LearnHandlers }) {
  const src = useSource();
  const a = props.answer;
  const flow = a.flow || [];
  const evidence = a.evidence || [];
  const sec = a.sections || {};
  const reasoning = sec.reasoning || [];
  const path = sec.execution_path || [];
  const related = sec.related || [];
  const assumptions = sec.assumptions || [];
  const verification = sec.verification || {};
  const runs = (a.run && a.run.runs) || [];

  // evidence ids (including "also" aliases) → evidence item, for the reasoning section's links
  const byId = createMemo(() => {
    const m = new Map<string, Evidence>();
    for (const e of evidence) {
      m.set(e.id, e);
      for (const alias of e.also || []) if (!m.has(alias)) m.set(alias, e);
    }
    return m;
  });

  const summary = () => {
    const counts = a.status_counts || {};
    const keys = [...CERTAINTY_ORDER, ...Object.keys(counts).filter((k) => !CERTAINTY_ORDER.includes(k))];
    return keys.filter((k) => counts[k]).map((k) => `${counts[k]} ${certainty(k).short}`);
  };

  const openEv = (e: Evidence) => e.file && src.open({ file: e.file, line: e.line, end: e.end_line });
  const learning = a.meta?.learning || {};
  const used = learning.used || [];
  const alternatives = learning.alternatives || [];

  return (
    <article class="answer" aria-label="Answer">
      <section class="a-sec a-question">
        <h2 class="eyebrow">Your question</h2>
        <p class="question-text">{a.question}</p>
      </section>

      <Show when={used.length || learning.pin_not_applied}>
        <div class="learned-notes" role="note" aria-label="What this answer learned from corrections">
          <For each={used}>{(u) => <LearnedNote use={u} onUndo={props.learn?.undo} busy={props.learn?.busy?.() || ""} />}</For>
          <Show when={learning.pin_not_applied}>
            {(m) => (
              <p class="learned-note muted">
                <span class="learned-tag">learned</span> Correction {m().id} points at <Inline text={m().target || "another place"} links={false} />,
                which this kind of question could not use, so this answer comes from the analysis alone.
              </p>
            )}
          </Show>
        </div>
      </Show>

      <section class="a-sec">
        <h2 class="eyebrow">Answer</h2>
        <div class="prose">
          <For each={a.answer || []}>
            {(p) => (
              <p>
                <Inline text={p} />
              </p>
            )}
          </For>
          <Show when={!(a.answer || []).length}>
            <p class="muted">No answer text was produced.</p>
          </Show>
        </div>
      </section>

      <Show when={flow.length}>
        <section class="a-sec">
          <h2 class="eyebrow">How the data flows</h2>
          <Limited
            each={flow}
            step={40}
            noun="steps"
            wrap={(list) => <ol class="flow">{list}</ol>}
          >
            {(s, i) => (
              <li class="flow-step" classList={{ first: i() === 0, last: i() === flow.length - 1 }}>
                <div class="flow-card">
                  <div class="flow-row">
                    <span class="flow-num" aria-hidden="true">{i() + 1}</span>
                    <span class="flow-label">
                      <Inline text={s.label} links={false} />
                    </span>
                    <LocLink file={s.file} line={s.line} />
                  </div>
                  <Show when={s.detail}>
                    <div class="flow-detail">
                      <Inline text={s.detail} />
                    </div>
                  </Show>
                </div>
              </li>
            )}
          </Limited>
        </section>
      </Show>

      <Show when={evidence.length}>
        <section class="a-sec">
          <h2 class="eyebrow">
            Evidence <span class="count">{evidence.length}</span>
          </h2>
          <Limited each={evidence} step={50} noun="pieces of evidence" wrap={(list) => <ul class="evidence">{list}</ul>}>
            {(e) => (
              <li>
                <button class="ev" onClick={() => openEv(e)} disabled={!e.file} title="Open this line in the code">
                  <span class="ev-head">
                    <span class="ev-loc">{loc(e.file, e.line, e.end_line)}</span>
                    <CertaintyBadge status={e.status} label={e.status_label} />
                  </span>
                  <Show when={e.text}>
                    <code class="ev-text">{e.text}</code>
                  </Show>
                  <Show when={e.note && !sameText(e.note, e.text || "")}>
                    <span class="ev-note">
                      <Inline text={e.note} links={false} />
                    </span>
                  </Show>
                </button>
              </li>
            )}
          </Limited>
        </section>
      </Show>

      <p class="certainty-line">
        <CertaintyBadge status={a.status} label={a.status_label} />
        <Show when={summary().length}>
          <span class="muted"> · {summary().join(" · ")}</span>
        </Show>
      </p>

      <Show when={props.learn && a.id}>
        <LearnCard />
      </Show>

      <div class="details">
        <Collapsible title="Show reasoning / evidence" count={reasoning.length || null}>
          <Show when={reasoning.length} fallback={<p class="muted">No separate reasoning steps for this answer.</p>}>
            <Limited each={reasoning} step={50} noun="statements" wrap={(list) => <ul class="claims">{list}</ul>}>
              {(c) => {
                const refs = () => {
                  const seen = new Set<string>();
                  const out: Array<{ id: string; ev?: Evidence }> = [];
                  for (const id of c.ev || []) {
                    const ev = byId().get(id);
                    const key = ev ? loc(ev.file, ev.line) : id;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({ id, ev });
                  }
                  return out;
                };
                return (
                  <li class="claim">
                    <p class="claim-text">
                      <Inline text={c.text} />
                    </p>
                    <div class="claim-meta">
                      <CertaintyBadge status={c.status} label={c.status_label} />
                      <span class={c.verified ? "verified ok" : "verified bad"}>
                        {c.verified ? "✓ verified" : "✗ not verified"}
                      </span>
                      <Show when={c.check}>
                        <span class="muted">{c.check}</span>
                      </Show>
                    </div>
                    <Show when={c.why}>
                      <p class="why">
                        <span class="muted">Why: </span>
                        <Inline text={c.why} />
                      </p>
                    </Show>
                    <Show when={refs().length}>
                      <div class="ev-refs">
                        <For each={refs().slice(0, 16)}>
                          {(r) => (
                            <button
                              class="ref"
                              disabled={!r.ev?.file}
                              onClick={() => r.ev && openEv(r.ev)}
                              title={r.ev?.text || r.id}
                            >
                              {r.ev ? loc(r.ev.file, r.ev.line) : r.id}
                            </button>
                          )}
                        </For>
                        <Show when={refs().length > 16}>
                          <span class="muted">+{refs().length - 16} more</span>
                        </Show>
                      </div>
                    </Show>
                  </li>
                );
              }}
            </Limited>
          </Show>
        </Collapsible>

        <Collapsible title="Show execution path" count={path.length || (runs.length ? plural(runs.length, "run") : null)}>
          <Show when={path.length} fallback={<p class="muted">No execution path for this answer.</p>}>
            <Limited each={path} step={60} noun="steps" wrap={(list) => <ol class="xpath">{list}</ol>}>
              {(s) => {
                const ran = s.status === "OBSERVED" || s.note === "ran";
                return (
                  <li class="xstep" classList={{ ran }}>
                    <span class="xlabel">
                      <Inline text={s.label} links={false} />
                    </span>
                    <Show when={ran}>
                      <span class="badge ran" title="Observed while running the code">ran</span>
                    </Show>
                    <Show when={s.note && s.note !== "ran"}>
                      <span class="muted xnote">{s.note}</span>
                    </Show>
                    <LocLink file={s.file} line={s.line} />
                  </li>
                );
              }}
            </Limited>
          </Show>
          <Show when={a.run}>
            <RunTable runs={runs} ok={a.run?.ok} error={a.run?.error || a.run?.explain} />
          </Show>
        </Collapsible>

        <Collapsible title="Show related code" count={related.length || null}>
          <Show when={related.length} fallback={<p class="muted">No related code listed.</p>}>
            <Limited each={related} step={50} noun="places" wrap={(list) => <ul class="related">{list}</ul>}>
              {(r) => (
                <li>
                  <button
                    class="related-item"
                    disabled={!r.file}
                    onClick={() => r.file && src.open({ file: r.file, line: r.line, end: r.end_line })}
                  >
                    <span class="related-label">
                      <Inline text={r.label} links={false} />
                    </span>
                    <span class="loc static">{loc(r.file, r.line, r.end_line)}</span>
                  </button>
                </li>
              )}
            </Limited>
          </Show>
        </Collapsible>

        <Collapsible title="Show assumptions" count={assumptions.length || null}>
          <Show when={assumptions.length} fallback={<p class="muted">No assumptions were needed for this answer.</p>}>
            <ul class="bullets">
              <For each={assumptions}>
                {(s) => (
                  <li>
                    <Inline text={s} />
                  </li>
                )}
              </For>
            </ul>
          </Show>
        </Collapsible>

        <Collapsible title="Show verification">
          <Verification answer={a} />
        </Collapsible>
      </div>
    </article>
  );

  function LearnCard() {
    const L = props.learn!;
    const busy = () => L.busy();
    return (
      <section class="a-sec learn-card" aria-label="Was this the right place?">
        <Show when={L.done()} fallback={
          <>
            <h2 class="eyebrow">Is this the right place?</h2>
            <Show when={alternatives.length}>
              <p class="learn-sub">Other places that could be the answer</p>
              <ol class="alts">
                <For each={alternatives}>
                  {(c) => (
                    <li class="alt">
                      <span class="alt-rank" aria-hidden="true">{c.rank}</span>
                      <span class="alt-label">
                        <Inline text={c.label} links={false} />
                      </span>
                      <LocLink file={c.file} line={c.line} />
                      <button class="btn sm" disabled={!!busy()} onClick={() => L.pick(c.rank)}
                        title="Record that this is the right place for this question">
                        This one
                      </button>
                    </li>
                  )}
                </For>
              </ol>
            </Show>
            <div class="learn-actions">
              <button class="btn sm ok-btn" disabled={!!busy()} onClick={() => L.confirm()}
                title="Keep this answer as a test case for this project">
                ✓ The answer was right
              </button>
              <Show when={learning.correctable}>
                <button class="btn sm subtle" disabled={!!busy()} onClick={() => L.pickLine()}
                  title="Open the code and pick the line that answers the question">
                  {alternatives.length ? "None of these: pick a line…" : "Not the right place: pick a line…"}
                </button>
              </Show>
              <Show when={busy()}>
                <span class="muted small" role="status"><Spinner /> {busy()}</span>
              </Show>
            </div>
            <p class="muted small learn-help">
              Your choice is saved for this project only. Later answers say when they use it, and you can undo it.
            </p>
          </>
        }>
          {(d) => (
            <p class="learn-done" role="status">
              <span class="ok">✓</span> {d().text}
            </p>
          )}
        </Show>
      </section>
    );
  }

  function Verification(p: { answer: Answer }) {
    const v = verification;
    const r = v.report || {};
    const stale = r.stale_files || [];
    const rejected = v.rejected || [];
    const meta = p.answer.meta || {};
    const wording = () => {
      const prose = meta.prose || {};
      const picked = meta.model_choices ? " The local AI model chose which of the places the analysis found to describe (see Assumptions)." : "";
      if (prose.mode === "model") return `Worded by the local AI model${prose.model ? ` (${prose.model})` : ""}; every fact was checked against the code.${picked}`;
      if (prose.mode === "model+facts") return `Summary by the local AI model${prose.model ? ` (${prose.model})` : ""}, followed by the complete checked facts.${picked}`;
      if (prose.fallback) return (typeof prose.fallback === "string" ? `Assembled directly from verified facts — ${prose.fallback}.` : "Assembled directly from verified facts (the AI model was not available).") + picked;
      if (prose.skipped) return "Shown exactly as checked (not reworded by the AI model)." + picked;
      return "Assembled directly from verified facts (no AI model used)." + picked;
    };
    return (
      <div class="verify">
        <ul class="verify-list">
          <Show when={r.claims != null}>
            <li>
              <span class={r.verified === r.claims ? "ok" : "warn-text"}>{r.verified === r.claims ? "✓" : "!"}</span>{" "}
              {r.verified ?? 0} of {plural(r.claims ?? 0, "statement")} re-checked against the code
            </li>
          </Show>
          <Show when={r.evidence != null}>
            <li>
              <span class={r.evidence_ok === r.evidence ? "ok" : "warn-text"}>{r.evidence_ok === r.evidence ? "✓" : "!"}</span>{" "}
              {r.evidence_ok ?? 0} of {plural(r.evidence ?? 0, "piece")} of evidence still match the code
            </li>
          </Show>
          <Show when={r.rejected}>
            <li>
              <span class="warn-text">!</span> {plural(r.rejected || 0, "statement was", "statements were")} left out because the code did not support{" "}
              {r.rejected === 1 ? "it" : "them"}
            </li>
          </Show>
          <li class="muted">
            {wording()}
            <Show when={meta.seconds != null}> Answered in {seconds(meta.seconds)}.</Show>
          </li>
        </ul>
        <Show when={stale.length}>
          <div class="banner warn">
            <div class="banner-row">
              <span class="banner-icon" aria-hidden="true">!</span>
              <div class="banner-text">
                {plural(stale.length, "file")} changed since Verity last read the project, so parts of this answer may be out of date:{" "}
                <For each={stale.slice(0, 10)}>{(f, i) => <><code class="ic">{f}</code>{i() < Math.min(stale.length, 10) - 1 ? ", " : ""}</>}</For>
                {stale.length > 10 ? ` and ${stale.length - 10} more` : ""}. Press Update at the top to refresh.
              </div>
            </div>
          </div>
        </Show>
        <Show when={rejected.length}>
          <h4 class="sub-h">Left out</h4>
          <ul class="bullets">
            <For each={rejected.slice(0, 30)}>{(c) => <li><Inline text={typeof c === "string" ? c : c.text || ""} /></li>}</For>
          </ul>
        </Show>
        <Show when={(v.notes || []).length}>
          <h4 class="sub-h">Notes</h4>
          <ul class="bullets">
            <For each={v.notes}>{(n) => <li><Inline text={n} /></li>}</For>
          </ul>
        </Show>
        <Show when={(v.unknowns || []).length}>
          <h4 class="sub-h">Could not be determined</h4>
          <ul class="bullets">
            <For each={v.unknowns}>{(n) => <li><Inline text={n} /></li>}</For>
          </ul>
        </Show>
      </div>
    );
  }
}

const MATCH_WORDS: Record<string, string> = {
  same: "this question",
  words: "a question about the same thing",
  meaning: "a question with the same meaning",
  similar: "a similar question",
};

/** How the question matched (answers stored before "match" existed: from how and meaning). */
function matchOf(u: LearningUse): string {
  if (u.match) return u.match;
  if (u.how !== "pin") return "similar";
  return u.meaning ? "meaning" : "same";
}

function LearnedNote(props: { use: LearningUse; onUndo?: (id: string) => void; busy: string }) {
  const u = props.use;
  const when = u.created ? dayText(u.created) : "";
  return (
    <p class="learned-note">
      <span class="learned-tag">learned</span>
      <span>
        {u.how === "pin" ? "Uses" : "Ranked with the help of"} correction <strong>{u.id}</strong>
        {when ? ` (${when})` : ""}, recorded for {MATCH_WORDS[matchOf(u)] || MATCH_WORDS.similar}
        <Show when={matchOf(u) !== "same" && u.question}>: “{u.question}”</Show>
        <Show when={u.note}>
          {" "}— <span class="learned-q">“{u.note}”</span>
        </Show>
        .
        <Show when={(u.conflicts || []).length}>
          <span class="warn-text"> It disagrees with {(u.conflicts || []).join(", ")}; the newest is used.</span>
        </Show>
      </span>
      <Show when={props.onUndo}>
        <button class="link-btn" disabled={!!props.busy} onClick={() => props.onUndo!(u.id)}
          title="Stop using this correction (you can restore it in Learned)">
          Undo
        </button>
      </Show>
    </p>
  );
}

function RunTable(props: { runs: Run[]; ok?: boolean; error?: string }) {
  const kv = (o: Record<string, unknown> | undefined) => Object.entries(o || {});
  return (
    <div class="runs">
      <h4 class="sub-h">
        {props.runs.length === 1 ? "What happened when the code ran" : `What happened in each of the ${props.runs.length} runs`}
      </h4>
      <p class="muted small">
        Verity compiled the real code and ran it in a sandbox. Hardware functions were replaced by stand-ins that record
        their calls.
      </p>
      <Show when={props.ok === false && props.error}>
        <p class="warn-text">{props.error}</p>
      </Show>
      <Show when={props.runs.length}>
        <div class="table-wrap">
          <table class="run-table">
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Inputs</th>
                <th scope="col">Outputs</th>
                <th scope="col">Final values</th>
              </tr>
            </thead>
            <tbody>
              <For each={props.runs}>
                {(r, i) => (
                  <tr>
                    <td data-h="Run">
                      <strong>{r.label || `Run ${i() + 1}`}</strong>
                      <Show when={r.ok === false}>
                        <div class="warn-text small">did not finish{r.error ? `: ${r.error}` : ""}</div>
                      </Show>
                      <Show when={r.seconds != null}>
                        <div class="muted small">{seconds(r.seconds)}</div>
                      </Show>
                    </td>
                    <td data-h="Inputs">
                      <For each={kv(r.inputs)} fallback={<span class="muted">–</span>}>
                        {([k, v]) => (
                          <div class="kv">
                            <span class="k">{k}</span> = <code class="ic">{valueText(v)}</code>
                          </div>
                        )}
                      </For>
                    </td>
                    <td data-h="Outputs">
                      <For each={r.hw_calls || []} fallback={<span class="muted">none</span>}>
                        {(c) => (
                          <div>
                            <code class="ic">{c}</code>
                          </div>
                        )}
                      </For>
                    </td>
                    <td data-h="Final values">
                      <For each={kv(r.state)} fallback={<span class="muted">–</span>}>
                        {([k, v]) => (
                          <div class="kv">
                            <span class="k">{k}</span> = <code class="ic">{valueText(v)}</code>
                          </div>
                        )}
                      </For>
                    </td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </div>
      </Show>
    </div>
  );
}
