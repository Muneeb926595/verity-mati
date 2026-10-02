// Declares the shell's own commands so they get ACL permissions (allow-engine-status, …). Tauri 2 only lets a
// remote page (our UI is served by the engine on http://127.0.0.1) call commands granted by a `remote` capability.
fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&["engine_status", "restart_engine", "pick_folder", "selftest_report"]),
    ))
    .expect("failed to run tauri-build");
}
