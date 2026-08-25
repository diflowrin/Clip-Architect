//! Supervises the Python backend that the UI talks to.
//!
//! The backend is the upstream MoneyPrinterTurbo FastAPI app, frozen with
//! PyInstaller and shipped next to the executable. This module owns its whole
//! lifecycle: choosing a port, laying out the writable data directory, starting
//! the process, waiting for it to accept connections, and killing it on exit.
//!
//! Paths are deliberately split in two, because the app is meant to install
//! read-only (Microsoft Store / MSIX puts the install root under
//! C:\Program Files\WindowsApps, which cannot be written to):
//!
//! * MPT_RESOURCE_DIR -> bundled fonts, songs and static assets, read-only.
//! * MPT_ROOT_DIR     -> per-user config.toml and storage/, writable.

use std::io;
use std::net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};

/// How long the backend gets to accept its first connection before we treat the
/// launch as failed.
///
/// Measured on this machine: 71s on a cold first run, 20s warm. The cold figure
/// is dominated by Windows Defender scanning the ~370 MB bundle, so a slower
/// disk can easily exceed it. The timeout only matters when something is
/// genuinely broken, and a premature failure is far worse than a slow success,
/// so it is set well clear of the observed worst case.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(300);
const STARTUP_POLL_INTERVAL: Duration = Duration::from_millis(100);

/// Ties the backend's lifetime to this process using a Windows Job Object.
///
/// `BackendProcess::shutdown` handles an orderly exit, but it only runs if the
/// app gets to run its exit handler. A crash, or "End task" from Task Manager,
/// skips it entirely and leaves the Python backend running: it keeps rendering,
/// keeps its port bound, and nothing ever reaps it. Observed in practice - an
/// orphan accumulated 25 minutes of CPU after its parent was killed.
///
/// A job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` makes the kernel terminate
/// the child when the last handle to the job closes, which happens when this
/// process dies for any reason.
#[cfg(windows)]
mod job {
    use windows::Win32::Foundation::{CloseHandle, HANDLE};
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows::Win32::System::Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE};

    /// Owns the job handle. Dropping it closes the handle, which is what makes
    /// the kernel kill the assigned process - so this must stay alive for as
    /// long as the app runs.
    pub struct JobHandle(HANDLE);

    // The handle is only ever closed on drop; Windows job handles are safe to
    // move between threads.
    unsafe impl Send for JobHandle {}
    unsafe impl Sync for JobHandle {}

    impl Drop for JobHandle {
        fn drop(&mut self) {
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }

    /// Creates a kill-on-close job and assigns `process_id` to it.
    pub fn assign(process_id: u32) -> Result<JobHandle, String> {
        unsafe {
            let job = CreateJobObjectW(None, None)
                .map_err(|error| format!("CreateJobObject failed: {error}"))?;
            let handle = JobHandle(job);

            let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const core::ffi::c_void,
                u32::try_from(std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>())
                    .map_err(|error| format!("job limit size overflow: {error}"))?,
            )
            .map_err(|error| format!("SetInformationJobObject failed: {error}"))?;

            let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, false, process_id)
                .map_err(|error| format!("OpenProcess failed: {error}"))?;
            let assigned = AssignProcessToJobObject(job, process);
            let _ = CloseHandle(process);
            assigned.map_err(|error| format!("AssignProcessToJobObject failed: {error}"))?;

            Ok(handle)
        }
    }
}

/// What `BackendProcess::liveness` found.
pub enum Liveness {
    Running,
    /// The child exited on its own. Carries the status as text, because
    /// `ExitStatus` is not worth threading through the status event.
    Exited(String),
    /// `shutdown` already took the child: the app is closing.
    Gone,
}

/// Managed state holding the running child so it can be stopped on exit.
#[derive(Default)]
pub struct BackendProcess {
    child: Mutex<Option<Child>>,
    /// Kept alive purely so the job object outlives the child; see `job`.
    #[cfg(windows)]
    _job: Mutex<Option<job::JobHandle>>,
}

impl BackendProcess {
    pub fn store(&self, child: Child) {
        // Assign to the job before recording the child, so an abnormal exit in
        // between still leaves the kernel responsible for cleanup.
        #[cfg(windows)]
        {
            match job::assign(child.id()) {
                Ok(handle) => {
                    if let Ok(mut guard) = self._job.lock() {
                        *guard = Some(handle);
                    }
                }
                // Not fatal: the orderly shutdown path still works, only the
                // crash case loses its safety net.
                Err(error) => log::warn!("backend not assigned to a job object: {error}"),
            }
        }

        if let Ok(mut guard) = self.child.lock() {
            *guard = Some(child);
        }
    }

    /// Whether the backend is still up, for the watcher in `lib.rs`.
    ///
    /// `Gone` and `Exited` are deliberately distinct: the child slot is only
    /// emptied by `shutdown`, which runs on the way out of the app, and
    /// reporting that as a crash would put a failure screen in front of a user
    /// who is closing the window.
    pub fn liveness(&self) -> Liveness {
        let mut guard = match self.child.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        let Some(child) = guard.as_mut() else {
            return Liveness::Gone;
        };
        match child.try_wait() {
            Ok(Some(status)) => Liveness::Exited(format!("{status}")),
            Ok(None) => Liveness::Running,
            // A child we cannot poll is one we cannot vouch for either, but
            // guessing "dead" from a transient OS error would tear down a
            // working session. Report it and keep watching.
            Err(error) => {
                log::warn!("cannot poll the backend process: {error}");
                Liveness::Running
            }
        }
    }

    /// Terminates the backend. Safe to call more than once.
    pub fn shutdown(&self) {
        // A poisoned mutex still hands back the child via into_inner, and
        // leaking the process would leave a stray server holding the port.
        let mut guard = match self.child.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        if let Some(mut child) = guard.take() {
            if let Err(error) = child.kill() {
                log::warn!("failed to kill backend process: {error}");
            }
            // Reap it so the OS does not keep a zombie entry around.
            let _ = child.wait();
        }
    }
}

/// Connection details handed to the frontend.
#[derive(Clone, serde::Serialize)]
pub struct BackendInfo {
    pub base_url: String,
    pub data_dir: String,
    /// The value every request to the backend has to present. See
    /// [`session_api_key`].
    pub api_key: String,
}

/// The origins the backend accepts cross-origin requests from.
///
/// A packaged webview only ever uses the three `tauri` ones. The Vite dev
/// server is an origin solely while `tauri dev` serves the frontend from it,
/// and shipping that entry in the release build was a real hole: 1420 is
/// Vite's default port, so any dev server the user happened to run in their
/// browser could read `/api/v1/settings` - every provider key - out of the
/// installed app.
/// The origins a packaged webview uses, and the only ones a release build sends.
const WEBVIEW_ORIGINS: &str = "tauri://localhost,http://tauri.localhost,https://tauri.localhost";

/// The above plus the Vite dev server. Keep in step with `build.devUrl` in
/// tauri.conf.json.
const DEV_ORIGINS: &str = "tauri://localhost,http://tauri.localhost,https://tauri.localhost,http://localhost:1420";

fn allowed_origins() -> &'static str {
    if cfg!(debug_assertions) {
        DEV_ORIGINS
    } else {
        WEBVIEW_ORIGINS
    }
}

/// A fresh API key for this launch, hex-encoded.
///
/// The backend binds to loopback, but loopback is not a trust boundary: every
/// process on the machine can reach it, and until this existed all of them
/// could read the user's provider keys straight out of `/api/v1/settings`. The
/// port being different every run is not an obstacle to anything that scans.
///
/// Generated here rather than persisted in config.toml for three reasons: a
/// key on disk stays valid after the app closes, it would be written back by
/// the backend's own `save_config`, and it would surface in the Settings form
/// as though the user had chosen it. This one lives in the child's environment
/// and dies with the process.
///
/// 32 bytes from the OS CSPRNG. The cost of getting this wrong is the whole
/// protection, and a guessable key is worse than none because it reads as
/// secure.
fn session_api_key() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes)
        .map_err(|error| format!("cannot generate a backend API key: {error}"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Reserves a free loopback port by binding to port 0 and reading back the
/// assignment. The listener is dropped before the backend starts, so there is a
/// small race window; on a desktop machine nothing else is racing for ports, and
/// the alternative (parsing the port out of the child's stdout) makes startup
/// failures much harder to diagnose.
fn reserve_port() -> io::Result<u16> {
    let listener = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))?;
    listener.local_addr().map(|addr| addr.port())
}

/// Opens the file the backend's stdout and stderr are redirected into.
///
/// The shell is a GUI process and starts the child with CREATE_NO_WINDOW, so
/// there is no console for it to inherit: `Stdio::inherit()` sent every Python
/// traceback, uvicorn line and loguru message to a handle nobody was reading.
/// An installed build that died left no evidence anywhere on disk, which is
/// exactly when evidence is needed - `tauri:dev` has a terminal and never
/// showed the problem.
fn open_backend_log(data_dir: &Path) -> io::Result<(Stdio, Stdio)> {
    let log_dir = data_dir.join("logs");
    std::fs::create_dir_all(&log_dir)?;

    // Keep one generation. The question is almost always "why did it die last
    // time", and the relaunch that asks it would otherwise overwrite the
    // answer before anyone could read it.
    let path = log_dir.join("backend.log");
    if path.exists() {
        let _ = std::fs::rename(&path, log_dir.join("backend.prev.log"));
    }

    let out = std::fs::File::create(&path)?;
    let err = out.try_clone()?;
    Ok((Stdio::from(out), Stdio::from(err)))
}

/// Copies config.example.toml into the data directory on first run.
///
/// The backend copies the example to config.toml itself when the latter is
/// missing, but it looks for the example next to config.toml - which is the
/// data directory, not the bundle. Seeding it here keeps that logic untouched.
fn seed_config_example(resource_dir: &Path, data_dir: &Path) -> io::Result<()> {
    let destination = data_dir.join("config.example.toml");
    if destination.exists() {
        return Ok(());
    }
    let source = resource_dir.join("config.example.toml");
    if !source.exists() {
        log::warn!("bundled config.example.toml is missing at {source:?}");
        return Ok(());
    }
    std::fs::copy(&source, &destination).map(|_| ())
}

/// Blocks until the backend accepts a TCP connection, or the timeout expires.
///
/// A successful connect is enough: uvicorn binds its socket only once the ASGI
/// app is constructed, so anything that would fail at import time has already
/// failed by this point.
fn wait_until_listening(port: u16, child: &mut Child) -> Result<(), String> {
    let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    let deadline = Instant::now() + STARTUP_TIMEOUT;

    while Instant::now() < deadline {
        // Check for a dead child first; otherwise a backend that crashes on
        // import would make us wait out the full timeout before reporting it.
        match child.try_wait() {
            Ok(Some(status)) => {
                return Err(format!("backend exited during startup with {status}"));
            }
            Ok(None) => {}
            Err(error) => return Err(format!("failed to poll backend process: {error}")),
        }

        if TcpStream::connect_timeout(&address.into(), STARTUP_POLL_INTERVAL).is_ok() {
            return Ok(());
        }
        std::thread::sleep(STARTUP_POLL_INTERVAL);
    }

    Err(format!(
        "backend did not start listening on port {port} within {}s",
        STARTUP_TIMEOUT.as_secs()
    ))
}

/// Builds the command that runs the backend.
///
/// Release builds run the PyInstaller binary shipped beside the executable.
/// Debug builds run sidecar.py from the local uv virtualenv instead, so the
/// Python side can be edited without re-freezing it on every change.
#[cfg(debug_assertions)]
fn backend_command(app: &AppHandle) -> Result<(Command, PathBuf), String> {
    let _ = app;
    let backend_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or("cannot locate the project root")?
        .join("backend");

    let python = backend_dir.join(".venv/Scripts/python.exe");
    if !python.exists() {
        return Err(format!(
            "dev virtualenv is missing at {python:?}; run `uv sync --python 3.11` in backend/"
        ));
    }

    let mut command = Command::new(python);
    command.arg(backend_dir.join("sidecar.py"));
    command.current_dir(&backend_dir);
    Ok((command, backend_dir.join("resource")))
}

#[cfg(not(debug_assertions))]
fn backend_command(app: &AppHandle) -> Result<(Command, PathBuf), String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("cannot locate the resource directory: {error}"))?;

    // The backend ships as a PyInstaller one-directory bundle, so the
    // executable must run with its `_internal` folder as a sibling.
    let backend_dir = resource_dir.join("backend");
    let binary = backend_dir.join("ca-desktop-backend.exe");
    if !binary.exists() {
        return Err(format!("backend binary is missing at {binary:?}"));
    }

    let mut command = Command::new(binary);
    command.current_dir(&backend_dir);
    Ok((command, resource_dir.join("resource")))
}

/// Starts the backend and returns the URL the frontend should call.
pub fn start(app: &AppHandle) -> Result<BackendInfo, String> {
    let data_dir = app
        .path()
        .app_local_data_dir()
        .map_err(|error| format!("cannot locate the data directory: {error}"))?;
    std::fs::create_dir_all(&data_dir)
        .map_err(|error| format!("cannot create the data directory {data_dir:?}: {error}"))?;

    let (mut command, resource_dir) = backend_command(app)?;

    seed_config_example(&resource_dir, &data_dir)
        .map_err(|error| format!("cannot seed the default configuration: {error}"))?;

    let port = reserve_port().map_err(|error| format!("cannot reserve a port: {error}"))?;
    let api_key = session_api_key()?;

    // Point the backend at the FFmpeg shipped in resources. This is the first
    // entry in its resolution order (app/utils/utils.py::get_ffmpeg_binary), so
    // it wins over whatever happens to be on the user's PATH - important
    // because the bundled build is deliberately LGPL, and picking up a random
    // system FFmpeg would make output codecs unpredictable.
    let bundled_ffmpeg = resource_dir.join("bin").join("ffmpeg.exe");
    if bundled_ffmpeg.exists() {
        command.env("IMAGEIO_FFMPEG_EXE", &bundled_ffmpeg);
        // moviepy reads FFMPEG_BINARY directly and only falls back to asking
        // imageio-ffmpeg. Setting both means the choice does not depend on
        // imageio-ffmpeg's internals, whose bundled binary is stripped from the
        // package anyway.
        command.env("FFMPEG_BINARY", &bundled_ffmpeg);
    } else {
        // Not fatal: the backend falls back to PATH, which is how a source
        // checkout without the downloaded binary still works.
        log::warn!("bundled ffmpeg not found at {bundled_ffmpeg:?}, falling back to PATH");
    }

    command
        .arg("--port")
        .arg(port.to_string())
        .env("MPT_ROOT_DIR", &data_dir)
        .env("MPT_RESOURCE_DIR", &resource_dir)
        // Authenticates the loopback API for this launch. Passed in the
        // environment rather than on the command line, which any other process
        // can read out of the process list.
        .env("MPT_APP_API_KEY", &api_key)
        // Restrict CORS to the origins a Tauri webview actually uses, rather
        // than leaving the backend's "*" default in place. The dev server is
        // only in this list in debug builds; see `allowed_origins`.
        .env("CORS_ALLOWED_ORIGINS", allowed_origins())
        // Unbuffered output keeps the child's logs in order when they are
        // interleaved into ours.
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null());

    // Debug builds run from a terminal that is showing this output live, and
    // sending it to a file instead would take away the thing that makes
    // `tauri:dev` worth using. Only the packaged build has nowhere to write.
    if cfg!(debug_assertions) {
        command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
    } else {
        match open_backend_log(&data_dir) {
            Ok((out, err)) => {
                command.stdout(out).stderr(err);
            }
            // Losing the log is not worth refusing to start over. Inheriting
            // is the old behaviour: harmless, just silent.
            Err(error) => {
                log::warn!("cannot open the backend log: {error}");
                command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
            }
        }
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW: without it the child opens a console window that
        // flashes over the app on every launch.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = command
        .spawn()
        .map_err(|error| format!("cannot start the backend: {error}"))?;

    if let Err(error) = wait_until_listening(port, &mut child) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }

    app.state::<BackendProcess>().store(child);
    log::info!("backend ready on port {port}, data dir {data_dir:?}");

    Ok(BackendInfo {
        base_url: format!("http://127.0.0.1:{port}"),
        data_dir: data_dir.to_string_lossy().into_owned(),
        api_key,
    })
}

#[cfg(test)]
mod tests {
    use super::{allowed_origins, session_api_key, DEV_ORIGINS, WEBVIEW_ORIGINS};

    /// The dev server entry shipped in the release build for a while, and 1420
    /// is Vite's default port - so any dev server the user happened to run
    /// could read /api/v1/settings out of the installed app. `cfg!` makes that
    /// regression invisible to `cargo check`, which is why it is asserted here.
    #[test]
    fn the_packaged_origin_list_has_no_dev_server() {
        assert!(!WEBVIEW_ORIGINS.contains("localhost:1420"));
        assert!(!WEBVIEW_ORIGINS.contains("127.0.0.1"));
    }

    #[test]
    fn the_dev_list_only_adds_to_the_packaged_one() {
        assert!(DEV_ORIGINS.starts_with(WEBVIEW_ORIGINS));
        assert!(DEV_ORIGINS.contains("http://localhost:1420"));
    }

    #[test]
    fn tests_run_against_the_dev_list() {
        // Guards the assertion above from being vacuous: `cargo test` builds
        // with debug_assertions, so this is the branch it exercises.
        assert_eq!(allowed_origins(), DEV_ORIGINS);
    }

    #[test]
    fn each_session_gets_a_different_key() {
        let first = session_api_key().expect("key generation failed");
        let second = session_api_key().expect("key generation failed");

        assert_eq!(first.len(), 64, "32 bytes, hex-encoded");
        assert!(first.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(first, second);
    }
}
