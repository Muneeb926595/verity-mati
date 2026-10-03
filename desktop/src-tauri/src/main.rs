// Verity desktop shell (Tauri 2), for macOS and Windows.
//
// On start it spawns the local engine
//     <ROOT>/.venv/bin/python -m verity.engine.server --port 0 --print-ready     (env VERITY_EXIT_WITH_PARENT=1)
//     (Windows: <ROOT>\.venv\Scripts\python.exe, no console window, env VERITY_STOP_ON_STDIN_EOF=1)
// reads the one-line ready JSON from its stdout, and points the main window at the UI the engine serves
// (http://127.0.0.1:<port>/?token=<token>). Same origin as the engine, so no CORS is involved; the engine's
// Origin check allows 127.0.0.1. The native folder picker is reachable from that page through a remote-URL
// capability (capabilities/engine-ui.json). The engine and everything it started are stopped when the app exits:
// on macOS through its process group (SIGTERM, then SIGKILL); on Windows by closing the engine's stdin, which also
// happens when the app is killed, then by ending its whole process tree.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, RunEvent, State, Url};

const READY_TIMEOUT: Duration = Duration::from_secs(60);
const STOP_GRACE: Duration = Duration::from_secs(8);
/// desktop/src-tauri at build time: the Verity folder is two levels up (used when the app runs from elsewhere).
const BUILD_DIR: &str = env!("CARGO_MANIFEST_DIR");
#[cfg(not(windows))]
const FALLBACK_ROOT: &str = "/opt/verity";
#[cfg(windows)]
const FALLBACK_ROOT: &str = r"C:\verity";
/// Windows: start console programs without a console window (the engine, and taskkill when stopping it).
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Process-group id of the running engine (0 = none). Read by the signal handler, so it must be an atomic.
static ENGINE_PGID: AtomicI32 = AtomicI32::new(0);

#[derive(Clone, Serialize)]
#[serde(tag = "state", rename_all = "lowercase")]
enum Status {
    Starting,
    Ready { url: String, port: u16 },
    Failed { message: String },
}

struct Engine {
    child: Option<Child>,
    status: Status,
    generation: u64,
    stopping: bool,
}

struct Shell {
    root: PathBuf,
    root_source: &'static str,
    log: Arc<Mutex<Option<File>>>,
    engine: Mutex<Engine>,
}

type ShellState = Arc<Shell>;

// ------------------------------------------------------------------------------------------ helpers
fn is_root(p: &Path) -> bool {
    p.join("verity").is_dir() && p.join(".venv").is_dir()
}

/// Windows: canonicalize() returns verbatim paths (\\?\C:\...), which the engine would inherit as its folder and data
/// folder; such paths do not compare equal to the C:\... form and some tools refuse them. Keep the plain form.
fn plain_path(p: PathBuf) -> PathBuf {
    if cfg!(windows) {
        let s = p.to_string_lossy();
        if let Some(rest) = s.strip_prefix(r"\\?\") {
            let b = rest.as_bytes();
            if b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && b[2] == b'\\' {
                return PathBuf::from(rest.to_string()); // drive paths only; \\?\UNC\... stays as it is
            }
        }
    }
    p
}

/// VERITY_ROOT if set (and valid), else walk up from the executable, else the folder it was built from, else the
/// default install location.
fn resolve_root() -> (PathBuf, &'static str) {
    if let Ok(v) = std::env::var("VERITY_ROOT") {
        let v = v.trim();
        if !v.is_empty() {
            let p = PathBuf::from(v);
            if is_root(&p) {
                return (p, "VERITY_ROOT");
            }
            eprintln!("[verity] VERITY_ROOT={v} has no verity/ and .venv/ — ignoring it");
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let exe = exe.canonicalize().map(plain_path).unwrap_or(exe);
        for dir in exe.ancestors().skip(1) {
            if is_root(dir) {
                return (dir.to_path_buf(), "executable location");
            }
        }
    }
    if let Some(dir) = Path::new(BUILD_DIR).ancestors().nth(2) {
        if is_root(dir) {
            return (dir.to_path_buf(), "build location");
        }
    }
    (PathBuf::from(FALLBACK_ROOT), "default location")
}

fn now_s() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn log_line(log: &Arc<Mutex<Option<File>>>, line: &str) {
    if let Ok(mut g) = log.lock() {
        if let Some(f) = g.as_mut() {
            let _ = writeln!(f, "{} {}", now_s(), line);
        }
    }
    if cfg!(debug_assertions) || std::env::var_os("VERITY_DESKTOP_VERBOSE").is_some() {
        eprintln!("[verity] {line}");
    }
}

/// GUI apps start with a minimal PATH; add the usual tool locations so the engine finds compilers etc.
/// (Windows gives GUI apps the full user PATH, which is kept as it is.)
#[cfg(windows)]
fn augmented_path() -> String {
    std::env::var("PATH").unwrap_or_default()
}

#[cfg(not(windows))]
fn augmented_path() -> String {
    let mut parts: Vec<String> = std::env::var("PATH")
        .unwrap_or_default()
        .split(':')
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect();
    for extra in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"] {
        if !parts.iter().any(|p| p == extra) {
            parts.push(extra.to_string());
        }
    }
    parts.join(":")
}

fn parse_ready(line: &str) -> Option<(u16, String)> {
    let v: serde_json::Value = serde_json::from_str(line.trim()).ok()?;
    if v.get("ready")?.as_bool()? {
        let port = v.get("port")?.as_u64()? as u16;
        let token = v.get("token")?.as_str()?.to_string();
        return Some((port, token));
    }
    None
}

#[cfg(unix)]
fn signal_group(pgid: i32, sig: i32) {
    if pgid > 0 {
        // SAFETY: plain syscalls on a process group we created
        unsafe {
            libc::kill(-pgid, sig);
        }
    }
}

/// Windows: close the engine's stdin (it then shuts down cleanly and stops its model server), wait for it, then end
/// whatever is left of its process tree (the venv launcher, the interpreter it started, a model server).
#[cfg(windows)]
fn terminate(child: &mut Child, log: &Arc<Mutex<Option<File>>>) {
    use std::os::windows::process::CommandExt;
    let pid = child.id();
    log_line(log, &format!("stopping engine pid {pid}"));
    drop(child.stdin.take());
    let deadline = Instant::now() + STOP_GRACE;
    loop {
        match child.try_wait() {
            Ok(Some(st)) => {
                log_line(log, &format!("engine exited ({st})"));
                return;
            }
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            _ => break,
        }
    }
    log_line(log, "engine did not stop in time; ending its process tree");
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .status();
    let _ = child.kill();
    let _ = child.wait();
}

/// SIGTERM the engine's process group, wait for a clean exit, then SIGKILL whatever is left.
#[cfg(unix)]
fn terminate(child: &mut Child, log: &Arc<Mutex<Option<File>>>) {
    let pid = child.id() as i32;
    log_line(log, &format!("stopping engine pid {pid}"));
    signal_group(pid, libc::SIGTERM);
    let deadline = Instant::now() + STOP_GRACE;
    loop {
        match child.try_wait() {
            Ok(Some(st)) => {
                log_line(log, &format!("engine exited ({st})"));
                break;
            }
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            _ => {
                log_line(log, "engine did not stop in time; killing it");
                signal_group(pid, libc::SIGKILL);
                let _ = child.kill();
                let _ = child.wait();
                break;
            }
        }
    }
    // anything else still in the engine's group (e.g. a model server) goes too
    signal_group(pid, libc::SIGKILL);
}

// ------------------------------------------------------------------------------------------ engine lifecycle
fn venv_python(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join(".venv").join("Scripts").join("python.exe")
    } else {
        root.join(".venv/bin/python")
    }
}

fn spawn_engine(shell: &Shell) -> Result<(Child, u16, String), String> {
    let python = venv_python(&shell.root);
    if !python.exists() {
        return Err(format!(
            "Verity's Python environment was not found:\n  {}\n\nVerity folder: {} (from {}).\nSet VERITY_ROOT to the folder that contains verity/ and .venv/.",
            python.display(),
            shell.root.display(),
            shell.root_source
        ));
    }
    let mut cmd = Command::new(&python);
    cmd.args(["-m", "verity.engine.server", "--port", "0", "--print-ready"])
        .current_dir(&shell.root)
        .env("VERITY_EXIT_WITH_PARENT", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("PATH", augmented_path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0); // its own group, so quitting can stop it and anything it started
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // the engine stops when this pipe closes (quit, crash or kill of the app); UTF-8 as in bin\verity.cmd
        cmd.stdin(Stdio::piped())
            .env("VERITY_STOP_ON_STDIN_EOF", "1")
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Could not start the engine ({}):\n{e}", python.display()))?;
    log_line(&shell.log, &format!("engine started: pid {} (root {} from {})", child.id(), shell.root.display(), shell.root_source));

    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let (tx, rx) = mpsc::channel::<Option<(u16, String)>>();
    let tail = Arc::new(Mutex::new(String::new()));

    {
        let log = shell.log.clone();
        thread::spawn(move || {
            let mut sent = false;
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if !sent {
                    if let Some(r) = parse_ready(&line) {
                        log_line(&log, &format!("engine ready on port {}", r.0));
                        let _ = tx.send(Some(r));
                        sent = true;
                        continue;
                    }
                }
                log_line(&log, &format!("engine: {line}"));
            }
            if !sent {
                let _ = tx.send(None);
            }
        });
    }
    {
        let log = shell.log.clone();
        let tail = tail.clone();
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines() {
                let Ok(line) = line else { break };
                log_line(&log, &format!("engine! {line}"));
                if let Ok(mut t) = tail.lock() {
                    t.push_str(&line);
                    t.push('\n');
                    if t.len() > 6000 {
                        let cut = t.len() - 4000;
                        let cut = (cut..t.len()).find(|i| t.is_char_boundary(*i)).unwrap_or(0);
                        t.drain(..cut);
                    }
                }
            }
        });
    }

    let last_output = |tail: &Arc<Mutex<String>>| -> String {
        thread::sleep(Duration::from_millis(300));
        let t = tail.lock().map(|t| t.trim().to_string()).unwrap_or_default();
        if t.is_empty() {
            String::new()
        } else {
            let lines: Vec<&str> = t.lines().collect();
            let from = lines.len().saturating_sub(25);
            format!("\n\nLast engine output:\n{}", lines[from..].join("\n"))
        }
    };

    match rx.recv_timeout(READY_TIMEOUT) {
        Ok(Some((port, token))) => Ok((child, port, token)),
        Ok(None) => {
            let status = child.wait().map(|s| s.to_string()).unwrap_or_default();
            Err(format!("The engine stopped before it was ready ({status}).{}", last_output(&tail)))
        }
        Err(_) => {
            terminate(&mut child, &shell.log);
            Err(format!(
                "The engine did not report that it was ready within {} seconds.{}",
                READY_TIMEOUT.as_secs(),
                last_output(&tail)
            ))
        }
    }
}

fn splash_url() -> Url {
    #[cfg(windows)]
    let u = "http://tauri.localhost/index.html";
    #[cfg(not(windows))]
    let u = "tauri://localhost/index.html";
    Url::parse(u).expect("static url")
}

fn start_engine(app: &AppHandle) {
    let shell: ShellState = app.state::<ShellState>().inner().clone();
    let generation = {
        let mut e = shell.engine.lock().unwrap();
        e.generation += 1;
        e.status = Status::Starting;
        e.stopping = false;
        e.generation
    };
    let app = app.clone();
    thread::spawn(move || match spawn_engine(&shell) {
        Ok((child, port, token)) => {
            let url = format!("http://127.0.0.1:{port}/?token={token}");
            let pid = child.id() as i32;
            {
                let mut e = shell.engine.lock().unwrap();
                if e.generation != generation || e.stopping {
                    // superseded (restart or quit) while starting
                    drop(e);
                    let mut child = child;
                    terminate(&mut child, &shell.log);
                    return;
                }
                e.child = Some(child);
                e.status = Status::Ready { url: url.clone(), port };
            }
            ENGINE_PGID.store(pid, Ordering::SeqCst);
            if let Some(w) = app.get_webview_window("main") {
                if let Ok(u) = Url::parse(&url) {
                    let _ = w.navigate(u);
                }
            }
            watch_engine(app, shell, generation);
        }
        Err(message) => {
            log_line(&shell.log, &format!("engine failed to start: {message}"));
            {
                let mut e = shell.engine.lock().unwrap();
                if e.generation == generation {
                    e.status = Status::Failed { message: message.clone() };
                }
            }
            if std::env::var_os("VERITY_DESKTOP_SELFTEST").is_some() {
                thread::spawn(move || {
                    thread::sleep(Duration::from_millis(2500)); // let the start-up page show the error
                    let first = message.lines().next().unwrap_or_default().to_string();
                    println!("SELFTEST engine-error {}", serde_json::json!({ "message": first }));
                    let _ = std::io::stdout().flush();
                    let shot = std::env::var_os("VERITY_DESKTOP_SNAPSHOT_DIR")
                        .map(|d| PathBuf::from(d).join("desktop-engine-error.png"));
                    if let (Some(p), Some(w)) = (shot.clone(), app.get_webview_window("main")) {
                        let _ = fs::remove_file(&p);
                        snapshot_webview(&w, p, shell.log.clone());
                    }
                    finish_selftest(app, shot);
                });
            }
        }
    });
}

/// If the engine dies on its own, go back to the start-up page, which offers "Try again".
fn watch_engine(app: AppHandle, shell: ShellState, generation: u64) {
    thread::spawn(move || loop {
        thread::sleep(Duration::from_millis(1000));
        let mut e = shell.engine.lock().unwrap();
        if e.generation != generation || e.stopping {
            return;
        }
        let exited = match e.child.as_mut() {
            Some(c) => match c.try_wait() {
                Ok(Some(st)) => Some(st.to_string()),
                Ok(None) => None,
                Err(err) => Some(err.to_string()),
            },
            None => return,
        };
        if let Some(st) = exited {
            e.child = None;
            ENGINE_PGID.store(0, Ordering::SeqCst);
            e.status = Status::Failed {
                message: format!(
                    "The engine stopped unexpectedly ({st}).\nDetails are in {}.",
                    Path::new("data").join("logs").join("desktop-engine.log").display()
                ),
            };
            drop(e);
            log_line(&shell.log, &format!("engine exited unexpectedly ({st})"));
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.navigate(splash_url());
            }
            return;
        }
    });
}

fn stop_engine(shell: &Shell) {
    let child = {
        let mut e = shell.engine.lock().unwrap();
        e.stopping = true;
        e.generation += 1;
        e.child.take()
    };
    ENGINE_PGID.store(0, Ordering::SeqCst);
    if let Some(mut child) = child {
        terminate(&mut child, &shell.log);
    }
}

// ------------------------------------------------------------------------------------------ commands (UI → shell)
#[tauri::command]
fn engine_status(shell: State<'_, ShellState>) -> Status {
    shell.engine.lock().unwrap().status.clone()
}

#[tauri::command]
fn restart_engine(app: AppHandle, shell: State<'_, ShellState>) {
    let shell = shell.inner().clone();
    thread::spawn(move || {
        stop_engine(&shell);
        start_engine(&app);
    });
}

/// Fallback folder picker (the UI normally uses @tauri-apps/plugin-dialog directly).
#[tauri::command]
async fn pick_folder(app: AppHandle) -> Option<String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = mpsc::channel();
    app.dialog()
        .file()
        .set_title("Choose your project folder")
        .pick_folder(move |p| {
            let _ = tx.send(p);
        });
    let picked = tauri::async_runtime::spawn_blocking(move || rx.recv().ok().flatten()).await.ok().flatten()?;
    picked.into_path().ok().map(|p| p.display().to_string())
}

/// Used by the self-test (VERITY_DESKTOP_SELFTEST=1|close|quit): the page reports what it could do at each stage.
/// With VERITY_DESKTOP_SNAPSHOT_DIR set, the web view is also captured to <dir>/desktop-<stage>.png (macOS).
#[tauri::command]
fn selftest_report(app: AppHandle, shell: State<'_, ShellState>, report: String, stage: String, last: bool) {
    log_line(&shell.log, &format!("SELFTEST {stage} {report}"));
    println!("SELFTEST {stage} {report}");
    let _ = std::io::stdout().flush();
    let shot = std::env::var_os("VERITY_DESKTOP_SNAPSHOT_DIR").map(|d| PathBuf::from(d).join(format!("desktop-{stage}.png")));
    if let (Some(path), Some(w)) = (shot.clone(), app.get_webview_window("main")) {
        let _ = fs::remove_file(&path);
        snapshot_webview(&w, path, shell.log.clone());
    }
    if last {
        finish_selftest(app, shot);
    }
}

/// After the last self-test stage: wait for the snapshot, then close the window or quit (VERITY_DESKTOP_SELFTEST=close|quit).
fn finish_selftest(app: AppHandle, shot: Option<PathBuf>) {
    let mode = std::env::var("VERITY_DESKTOP_SELFTEST").unwrap_or_default();
    if mode != "close" && mode != "quit" {
        return;
    }
    thread::spawn(move || {
        if let Some(p) = shot {
            let t0 = Instant::now();
            while !p.exists() && t0.elapsed() < Duration::from_secs(8) {
                thread::sleep(Duration::from_millis(100));
            }
            thread::sleep(Duration::from_millis(300));
        }
        if mode == "close" {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.close();
            }
        } else {
            app.exit(0);
        }
    });
}

#[cfg(target_os = "macos")]
fn snapshot_webview(w: &tauri::WebviewWindow, out: PathBuf, log: Arc<Mutex<Option<File>>>) {
    let res = w.with_webview(move |pw| {
        use block2::RcBlock;
        use objc2::MainThreadMarker;
        use objc2_app_kit::{NSBitmapImageFileType, NSBitmapImageRep, NSImage};
        use objc2_foundation::{NSDictionary, NSError};
        use objc2_web_kit::{WKSnapshotConfiguration, WKWebView};
        let Some(mtm) = MainThreadMarker::new() else { return };
        // SAFETY: on macOS the platform web view is a WKWebView, and this closure runs on the main thread
        unsafe {
            let wk: &WKWebView = &*(pw.inner() as *const WKWebView);
            let cfg = WKSnapshotConfiguration::new(mtm);
            let block = RcBlock::new(move |image: *mut NSImage, _err: *mut NSError| {
                if image.is_null() {
                    log_line(&log, "snapshot failed (no image)");
                    return;
                }
                let image = &*image;
                let png = image
                    .TIFFRepresentation()
                    .and_then(|tiff| NSBitmapImageRep::imageRepWithData(&tiff))
                    .and_then(|rep| rep.representationUsingType_properties(NSBitmapImageFileType::PNG, &NSDictionary::new()));
                match png {
                    Some(data) => {
                        let _ = fs::write(&out, data.to_vec());
                        log_line(&log, &format!("snapshot written: {}", out.display()));
                    }
                    None => log_line(&log, "snapshot failed (encoding)"),
                }
            });
            wk.takeSnapshotWithConfiguration_completionHandler(Some(&cfg), &block);
        }
    });
    if let Err(e) = res {
        eprintln!("[verity] snapshot failed: {e}");
    }
}

#[cfg(not(target_os = "macos"))]
fn snapshot_webview(_w: &tauri::WebviewWindow, _out: PathBuf, _log: Arc<Mutex<Option<File>>>) {}

const SELFTEST_JS: &str = r#"
(async (project, question) => {
  const T = window.__TAURI_INTERNALS__;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (f, n = 120) => { for (let i = 0; i < n; i++) { try { if (f()) return true; } catch {} await wait(250); } return false; };
  const report = (stage, r, last) => T.invoke('selftest_report', { report: JSON.stringify(r), stage, last });
  await until(() => document.querySelector('.hero-title') && document.querySelectorAll('.recent-item, .empty').length);
  const r = {
    href: location.origin + location.pathname,
    tauri: !!T,
    title: document.title,
    hero: document.querySelector('.hero-title')?.innerText || null,
    subtitle: document.querySelector('.hero-sub')?.innerText || null,
    openProjectButton: !![...document.querySelectorAll('button')].find((b) => b.innerText.trim() === 'Open Project'),
    pathField: !!document.querySelector('#folder-path'),
    recentProjects: document.querySelectorAll('.recent-item').length,
  };
  try { await T.invoke('plugin:dialog|open', { options: 42 }); r.dialogAcl = 'call went through'; }
  catch (e) { const s = String(e); r.dialogAcl = /not allowed|denied|permission/i.test(s) ? 'DENIED: ' + s : 'allowed (arguments rejected as expected)'; }
  try { r.engineStatus = (await T.invoke('engine_status')).state; } catch (e) { r.engineStatus = 'ERR ' + e; }
  await report('home', r, !project);
  if (!project) return;
  await wait(1500);
  location.hash = '#/project/' + encodeURIComponent(project);
  const a = { project };
  a.ready = await until(() => document.querySelector('.proj-actions .pill')?.innerText.trim() === 'Ready', 240);
  const input = document.querySelector('.ask-input');
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, question);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  a.answered = await until(() => document.querySelector('.answer .eyebrow') && !document.querySelector('.ask-progress'), 480);
  a.sections = [...document.querySelectorAll('.answer .eyebrow')].map((e) => e.childNodes[0].textContent.trim());
  a.flowSteps = document.querySelectorAll('.flow-step').length;
  a.evidence = document.querySelectorAll('.ev').length;
  a.firstParagraph = (document.querySelector('.answer .prose p')?.innerText || '').slice(0, 120);
  window.scrollTo(0, 0);
  await wait(600);
  await report('answer', a, true);
})
"#;

// ------------------------------------------------------------------------------------------ signals
// Windows needs no handler: however the app ends, the engine's stdin pipe closes and the engine stops.
#[cfg(unix)]
extern "C" fn on_signal(sig: libc::c_int) {
    let pgid = ENGINE_PGID.load(Ordering::SeqCst);
    if pgid > 0 {
        // SAFETY: kill() and _exit() are async-signal-safe
        unsafe {
            libc::kill(-pgid, libc::SIGTERM);
        }
    }
    unsafe {
        libc::_exit(128 + sig);
    }
}

#[cfg(not(unix))]
fn install_signal_handlers() {}

#[cfg(unix)]
fn install_signal_handlers() {
    // SAFETY: installing a handler that only calls async-signal-safe functions
    let handler = on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t;
    unsafe {
        libc::signal(libc::SIGINT, handler);
        libc::signal(libc::SIGTERM, handler);
        libc::signal(libc::SIGHUP, handler);
    }
}

// ------------------------------------------------------------------------------------------ main
fn main() {
    let (root, root_source) = resolve_root();
    let log_file = {
        let dir = root.join("data").join("logs");
        let _ = fs::create_dir_all(&dir);
        OpenOptions::new().create(true).append(true).open(dir.join("desktop-engine.log")).ok()
    };
    let shell: ShellState = Arc::new(Shell {
        root,
        root_source,
        log: Arc::new(Mutex::new(log_file)),
        engine: Mutex::new(Engine { child: None, status: Status::Starting, generation: 0, stopping: false }),
    });
    log_line(&shell.log, &format!("desktop shell starting (Verity folder {} from {})", shell.root.display(), shell.root_source));
    install_signal_handlers();

    let selftest = std::env::var_os("VERITY_DESKTOP_SELFTEST").is_some();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(shell.clone())
        .invoke_handler(tauri::generate_handler![engine_status, restart_engine, pick_folder, selftest_report])
        .on_page_load(move |webview, payload| {
            if selftest && payload.event() == PageLoadEvent::Finished && payload.url().host_str() == Some("127.0.0.1") {
                let w = webview.clone();
                thread::spawn(move || {
                    thread::sleep(Duration::from_millis(1500));
                    let project = std::env::var("VERITY_DESKTOP_SELFTEST_PROJECT").unwrap_or_default();
                    let question = std::env::var("VERITY_DESKTOP_SELFTEST_QUESTION")
                        .unwrap_or_else(|_| "Explain how GPS data travels through this application.".into());
                    let js = format!(
                        "{}({}, {});",
                        SELFTEST_JS.trim(),
                        serde_json::to_string(&project).unwrap_or_default(),
                        serde_json::to_string(&question).unwrap_or_default()
                    );
                    let _ = w.eval(&js);
                });
            }
        })
        .setup(|app| {
            start_engine(app.handle());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Verity app");

    app.run(move |_handle, event| {
        if let RunEvent::Exit = event {
            stop_engine(&shell);
            log_line(&shell.log, "desktop shell exited");
        }
    });
}
