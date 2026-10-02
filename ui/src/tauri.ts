// Desktop (Tauri 2) integration. In browser mode none of this exists and the UI falls back to a text field.

type TauriInternals = { invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown> };

function internals(): TauriInternals | undefined {
  return (window as unknown as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
}

export const isTauri = (): boolean => !!internals()?.invoke;

/**
 * Native folder picker. Uses @tauri-apps/plugin-dialog; if the plugin call is refused (for example by a
 * permission problem) it falls back to the shell's own `pick_folder` command. Returns null when cancelled.
 */
export async function pickFolder(): Promise<string | null> {
  try {
    const { open } = await import("@tauri-apps/plugin-dialog");
    const r = await open({ directory: true, multiple: false, title: "Choose your project folder" });
    if (Array.isArray(r)) return r[0] ?? null;
    return r ?? null;
  } catch (first) {
    const t = internals();
    if (!t) throw first;
    const r = await t.invoke("pick_folder");
    return typeof r === "string" && r ? r : null;
  }
}
