// Two screens only: Home and a Project. The project path lives in the URL hash so a reload keeps you where you were
// (the ?token=… query string is left untouched).
import { createSignal } from "solid-js";
import type { ProjectStatus } from "./types";

export type Route = { name: "home" } | { name: "project"; path: string };

function parse(): Route {
  const h = location.hash;
  if (h.startsWith("#/project/")) {
    try {
      const path = decodeURIComponent(h.slice("#/project/".length));
      if (path) return { name: "project", path };
    } catch {
      /* fall through */
    }
  }
  return { name: "home" };
}

const [route, setRoute] = createSignal<Route>(parse());
export { route };

window.addEventListener("hashchange", () => setRoute(parse()));

export function goProject(path: string) {
  location.hash = "#/project/" + encodeURIComponent(path);
}

export function goHome() {
  if (location.hash) history.pushState(null, "", location.pathname + location.search);
  setRoute({ name: "home" });
}

// The Home screen already called project.open; hand the result over so the project view does not wait twice.
let opened: { status: ProjectStatus; at: number } | null = null;
export function rememberOpened(status: ProjectStatus) {
  opened = { status, at: Date.now() };
}
export function takeOpened(path: string): ProjectStatus | null {
  const o = opened;
  opened = null;
  if (o && o.status.path === path && Date.now() - o.at < 3000) return o.status;
  return null;
}
