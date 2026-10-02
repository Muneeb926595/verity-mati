import { createSignal, Match, onCleanup, onMount, Show, Switch } from "solid-js";
import { connectEvents, engineReachable, errorText, ping, rpc, RpcError, sseState, UNAUTHORIZED } from "../api";
import { goHome, route } from "../router";
import { Spinner } from "./common";
import Home from "./Home";
import ProjectView from "./ProjectView";
import Settings from "./Settings";

type Boot = "starting" | "ready" | "blocked";

export default function App() {
  const [boot, setBoot] = createSignal<Boot>("starting");
  const [blockedMsg, setBlockedMsg] = createSignal("");
  const [slow, setSlow] = createSignal(false);
  const [settingsOpen, setSettingsOpen] = createSignal(false);
  let alive = true;
  onCleanup(() => (alive = false));

  async function waitForEngine() {
    const t0 = Date.now();
    let delay = 400;
    while (alive) {
      try {
        await rpc("job.list");
        setBoot("ready");
        return;
      } catch (e) {
        if (e instanceof RpcError && (e.code === UNAUTHORIZED || e.code === 403)) {
          setBlockedMsg(e.message);
          setBoot("blocked");
          return;
        }
        if (!(e instanceof RpcError && e.network)) {
          // the engine answered but something else went wrong; show it rather than spinning forever
          if (await ping()) {
            setBlockedMsg(errorText(e));
            setBoot("blocked");
            return;
          }
        }
      }
      if (Date.now() - t0 > 15000) setSlow(true);
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 1.5, 3000);
    }
  }

  onMount(async () => {
    await waitForEngine();
    if (boot() === "ready") connectEvents();
  });

  const reconnecting = () => boot() === "ready" && (sseState() === "reconnecting" || !engineReachable());

  return (
    <div class="app">
      <header class="topbar">
        <Show when={route().name === "project"}>
          <button class="brand" onClick={goHome} title="Back to your projects">
            <span class="brand-mark" aria-hidden="true" />
            Verity
          </button>
        </Show>
        <div class="topbar-spacer" />
        <Show when={reconnecting()}>
          <span class="conn-pill" role="status">
            <Spinner /> Reconnecting…
          </span>
        </Show>
        <Show when={boot() === "ready"}>
          <button class="icon-btn gear" aria-label="Settings" title="Settings" onClick={() => setSettingsOpen(true)}>
            <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
              <path
                fill="currentColor"
                d="M19.14 12.94a7.1 7.1 0 0 0 .05-.94 7.1 7.1 0 0 0-.05-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.03 7.03 0 0 0-1.63-.94l-.36-2.54A.5.5 0 0 0 13.9 2h-3.84a.5.5 0 0 0-.49.42l-.36 2.54c-.59.24-1.13.56-1.63.94l-2.39-.96a.5.5 0 0 0-.61.22L2.66 8.48a.5.5 0 0 0 .12.64l2.03 1.58a7.1 7.1 0 0 0 0 1.88l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.3.61.22l2.39-.96c.5.38 1.04.7 1.63.94l.36 2.54c.05.24.25.42.49.42h3.84c.24 0 .45-.18.49-.42l.36-2.54c.59-.24 1.13-.56 1.63-.94l2.39.96c.22.08.48 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58ZM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7Z"
              />
            </svg>
          </button>
        </Show>
      </header>

      <Switch>
        <Match when={boot() === "starting"}>
          <div class="center-screen">
            <Spinner size="lg" />
            <h2>Starting the engine…</h2>
            <p class="muted">
              {slow() ? "This is taking longer than usual. Verity keeps trying — you can leave this window open." : "This usually takes a few seconds."}
            </p>
          </div>
        </Match>
        <Match when={boot() === "blocked"}>
          <div class="center-screen">
            <div class="big-icon" aria-hidden="true">!</div>
            <h2>Verity can't connect</h2>
            <p class="muted narrow">{blockedMsg()}</p>
            <button class="btn" onClick={() => location.reload()}>
              Try again
            </button>
          </div>
        </Match>
        <Match when={boot() === "ready"}>
          <Switch>
            <Match when={route().name === "home"}>
              <Home />
            </Match>
            <Match when={route().name === "project" && (route() as { path: string }).path} keyed>
              {(path) => <ProjectView path={path} />}
            </Match>
          </Switch>
        </Match>
      </Switch>

      <Show when={settingsOpen()}>
        <Settings onClose={() => setSettingsOpen(false)} />
      </Show>
    </div>
  );
}
