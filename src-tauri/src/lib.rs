mod backend;

use std::sync::Mutex;
use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager, RunEvent};

use backend::{BackendInfo, BackendProcess, Liveness};

/// Startup progress, polled by the frontend and pushed as an event.
///
/// Freezing a Python interpreter with moviepy and litellm in it takes several
/// seconds to load, so the window opens immediately and renders against this
/// state instead of blocking on a ready backend.
#[derive(Clone, serde::Serialize)]
#[serde(tag = "state", rename_all = "camelCase")]
pub enum BackendStatus {
    Starting,
    Ready(BackendInfo),
    Failed { message: String },
    /// Started, served, and then exited on its own. Distinct from `Failed`,
    /// which means it never came up: the two need different advice, and
    /// telling someone mid-session that the engine "failed to start" sends
    /// them looking for a startup problem that never happened.
    Stopped { message: String },
}

#[derive(Default)]
struct BackendStatusState(Mutex<Option<BackendStatus>>);

const BACKEND_STATUS_EVENT: &str = "backend://status";

#[tauri::command]
fn backend_status(app: AppHandle) -> BackendStatus {
    let state = app.state::<BackendStatusState>();
    let guard = match state.0.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    guard.clone().unwrap_or(BackendStatus::Starting)
}

fn publish_status(app: &AppHandle, status: BackendStatus) {
    {
        let state = app.state::<BackendStatusState>();
        let mut guard = match state.0.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        *guard = Some(status.clone());
    }

    // The frontend may not have registered its listener yet; backend_status()
    // covers that case, so a failed emit is not worth surfacing to the user.
    if let Err(error) = app.emit(BACKEND_STATUS_EVENT, &status) {
        log::warn!("failed to emit backend status: {error}");
    }
}

/// Watches the backend for the rest of the session and reports it dying.
///
/// `backend://status` used to be published exactly once, at startup, so the
/// sidebar went on saying the backend was running however long ago it had
/// exited. The only symptom was every request failing, with a stale "running"
/// line underneath contradicting it.
///
/// The check is `try_wait` on the child rather than a request to the port: it
/// costs nothing, needs no HTTP client in the shell, and answers the question
/// that matters. A backend that is alive but wedged is a different problem,
/// and one the frontend's own failed requests already surface.
fn watch_backend(app: AppHandle) {
    const INTERVAL: Duration = Duration::from_secs(2);

    loop {
        std::thread::sleep(INTERVAL);
        match app.state::<BackendProcess>().liveness() {
            Liveness::Running => {}
            // The app is shutting down. Nothing to report, and nobody to
            // report it to.
            Liveness::Gone => return,
            Liveness::Exited(status) => {
                let message = format!(
                    "The rendering engine stopped on its own ({status}). \
                     Its output is in logs/backend.log in the app data folder."
                );
                log::error!("backend exited unexpectedly: {status}");
                publish_status(&app, BackendStatus::Stopped { message });
                return;
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // Only the opener plugin: it is what "Open video" and "Show in folder"
        // call. The shell and dialog plugins were registered here too and used
        // by nothing - no capability granted them anything, so they were not
        // exploitable, but a registered shell plugin is one mistaken
        // capability entry away from turning any webview bug into command
        // execution. Cheaper to not have it.
        .plugin(tauri_plugin_opener::init())
        .manage(BackendProcess::default())
        .manage(BackendStatusState::default())
        .invoke_handler(tauri::generate_handler![backend_status])
        .setup(|app| {
            // Registered in release too, deliberately. Gating this on
            // debug_assertions meant the installed build - the only one users
            // run, and the only one that can fail in ways dev never sees -
            // wrote no log at all. Diagnosing it came down to guesswork.
            app.handle().plugin(
                tauri_plugin_log::Builder::default()
                    .level(log::LevelFilter::Info)
                    // The plugin stamps UTC by default, while the Python side
                    // logs local time. Diagnosing a startup failure means
                    // reading both files side by side, and an unexplained
                    // offset between them is exactly the kind of detail that
                    // sends someone looking at the wrong minute.
                    .timezone_strategy(tauri_plugin_log::TimezoneStrategy::UseLocal)
                    // One line per launch, so the file grows slowly; the cap
                    // is there for a crash loop, not for normal use.
                    .max_file_size(2_000_000)
                    .rotation_strategy(tauri_plugin_log::RotationStrategy::KeepOne)
                    .build(),
            )?;

            // Starting the backend blocks for as long as the interpreter takes
            // to come up. Doing that on the main thread would leave the window
            // unpainted, so it runs off-thread and reports via events.
            let handle = app.handle().clone();
            std::thread::spawn(move || match backend::start(&handle) {
                Ok(info) => {
                    publish_status(&handle, BackendStatus::Ready(info));
                    watch_backend(handle);
                }
                Err(message) => {
                    log::error!("backend failed to start: {message}");
                    publish_status(&handle, BackendStatus::Failed { message });
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            // Exit and ExitRequested both fire in normal shutdowns, but only
            // Exit is guaranteed for every path; shutdown() is idempotent, so
            // handling both simply makes the kill happen as early as possible.
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                app.state::<BackendProcess>().shutdown();
            }
        });
}
