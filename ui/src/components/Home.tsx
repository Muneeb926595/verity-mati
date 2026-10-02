import { createSignal, For, onCleanup, onMount, Show } from "solid-js";
import { errorText, onReconnect, rpc } from "../api";
import { goProject, rememberOpened } from "../router";
import { isTauri, pickFolder } from "../tauri";
import type { ProjectStatus, RecentProject } from "../types";
import { languagesLabel, shortPath, timeAgo } from "../util";
import { Banner, Spinner } from "./common";

export default function Home() {
  const tauri = isTauri();
  const [recent, setRecent] = createSignal<RecentProject[] | null>(null);
  const [recentError, setRecentError] = createSignal<string | null>(null);
  const [path, setPath] = createSignal("");
  const [error, setError] = createSignal<string | null>(null);
  const [busy, setBusy] = createSignal<string | null>(null);
  const [showField, setShowField] = createSignal(!tauri);
  let field: HTMLInputElement | undefined;

  async function loadRecent() {
    try {
      setRecent(await rpc<RecentProject[]>("project.recent"));
      setRecentError(null);
    } catch (e) {
      setRecentError(errorText(e));
      if (recent() === null) setRecent([]);
    }
  }

  onMount(() => {
    loadRecent();
    if (!tauri) field?.focus();
  });
  onCleanup(onReconnect(loadRecent));

  async function openPath(p: string) {
    p = p.trim();
    if (!p) {
      setError("Type the full path of your project folder, for example /Users/you/code/my-project.");
      field?.focus();
      return;
    }
    setBusy(p);
    setError(null);
    try {
      const st = await rpc<ProjectStatus>("project.open", { path: p });
      rememberOpened(st);
      goProject(st.path);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  async function pick() {
    setError(null);
    try {
      const p = await pickFolder();
      if (p) await openPath(p);
    } catch (e) {
      setError("The folder picker could not be opened (" + errorText(e) + "). You can type the folder path instead.");
      setShowField(true);
      queueMicrotask(() => field?.focus());
    }
  }

  async function forget(r: RecentProject, ev: MouseEvent) {
    ev.stopPropagation();
    setRecent((list) => (list || []).filter((x) => x.path !== r.path));
    try {
      await rpc("project.forget", { path: r.path });
    } catch (e) {
      setError(errorText(e));
      loadRecent();
    }
  }

  return (
    <main class="home">
      <div class="hero">
        <div class="hero-mark" aria-hidden="true" />
        <h1 class="hero-title">Verity</h1>
        <p class="hero-sub">Understand your C/C# codebase</p>

        <Show when={tauri}>
          <button class="btn primary xl" onClick={pick} disabled={!!busy()}>
            <Show when={busy()} fallback={"Open Project"}>
              <Spinner /> Opening…
            </Show>
          </button>
          <Show when={!showField()}>
            <button class="link-btn subtle-link" onClick={() => { setShowField(true); queueMicrotask(() => field?.focus()); }}>
              or type a folder path
            </button>
          </Show>
        </Show>

        <Show when={showField()}>
          <form
            class="path-form"
            onSubmit={(e) => {
              e.preventDefault();
              openPath(path());
            }}
          >
            <label class="field-label" for="folder-path">
              Folder path
            </label>
            <div class="path-row">
              <input
                id="folder-path"
                ref={field}
                class="input"
                type="text"
                placeholder="/Users/you/code/my-project"
                autocomplete="off"
                spellcheck={false}
                value={path()}
                onInput={(e) => setPath(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.isComposing) {
                    e.preventDefault();
                    openPath(e.currentTarget.value);
                  }
                }}
              />
              <button class="btn primary" type="submit" disabled={!!busy()}>
                <Show when={busy()} fallback={tauri ? "Open" : "Open Project"}>
                  <Spinner /> Opening…
                </Show>
              </button>
            </div>
          </form>
        </Show>

        <Show when={error()}>
          <Banner kind="error" onClose={() => setError(null)}>
            {error()}
          </Banner>
        </Show>
      </div>

      <section class="recent" aria-labelledby="recent-h">
        <h2 id="recent-h" class="section-title">
          Recent projects
        </h2>
        <Show when={recent() !== null} fallback={<div class="muted pad"><Spinner /> Loading…</div>}>
          <Show when={recentError()}>
            <Banner kind="warn">{recentError()}</Banner>
          </Show>
          <Show
            when={(recent() || []).length}
            fallback={<p class="muted empty">Projects you open will appear here.</p>}
          >
            <ul class="recent-list">
              <For each={recent()}>
                {(r) => {
                  const langs = languagesLabel(r.languages);
                  const ready = !!r.ready && (r.label || "").startsWith("Ready");
                  return (
                    <li>
                      <div
                        class="recent-item"
                        classList={{ missing: !r.exists, busy: busy() === r.path }}
                        role="button"
                        tabindex={r.exists ? 0 : -1}
                        aria-disabled={!r.exists}
                        onClick={() => r.exists && openPath(r.path)}
                        onKeyDown={(e) => {
                          if ((e.key === "Enter" || e.key === " ") && r.exists) {
                            e.preventDefault();
                            openPath(r.path);
                          }
                        }}
                        title={r.exists ? `Open ${r.path}` : "This folder no longer exists"}
                      >
                        <div class="recent-main">
                          <div class="recent-name">{r.name}</div>
                          <div class="recent-path">{shortPath(r.path)}</div>
                        </div>
                        <div class="recent-meta">
                          <Show when={langs}>
                            <span class="lang-tag">{langs}</span>
                          </Show>
                          <Show
                            when={r.exists}
                            fallback={<span class="pill gray">Folder not found</span>}
                          >
                            <span class={`pill ${ready ? "green" : r.label?.startsWith("Indexing") ? "amber" : "gray"}`}>
                              <Show when={ready}>
                                <span class="pill-dot" aria-hidden="true" />
                              </Show>
                              {r.label?.startsWith("Ready —") ? r.label.replace(" since the last index", "") : r.label || "Not indexed yet"}
                            </span>
                          </Show>
                          <Show when={r.opened}>
                            <span class="recent-when">{timeAgo(r.opened)}</span>
                          </Show>
                          <Show when={busy() === r.path}>
                            <Spinner />
                          </Show>
                        </div>
                        <button
                          class="icon-btn forget"
                          aria-label={`Forget ${r.name}`}
                          title="Remove from this list (nothing is deleted)"
                          onClick={(e) => forget(r, e)}
                        >
                          ×
                        </button>
                      </div>
                    </li>
                  );
                }}
              </For>
            </ul>
          </Show>
        </Show>
      </section>
      <footer class="home-foot muted">Everything runs on this computer. Your code never leaves it.</footer>
    </main>
  );
}
