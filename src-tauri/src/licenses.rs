//! The licence texts shipped in `licenses/`, read for the Licenses dialog.
//!
//! They are rendered inside the app rather than opened in Explorer on purpose:
//! an MSIX installs under `WindowsApps`, whose ACLs stop the user from browsing
//! the folder at all. "Show in folder" would hand a certification reviewer an
//! access-denied dialog where the notices are supposed to be.

use std::fs;
use std::path::PathBuf;

use tauri::AppHandle;

/// The shipped files in the order they are worth reading, with the title the
/// dialog shows. A file present in the folder but missing from this table
/// still appears, under its own name: the packaging script decides what must
/// ship, and a licence listed without a nice title beats one that ships
/// invisible.
const DOCUMENTS: &[(&str, &str)] = &[
    ("THIRD-PARTY-NOTICES.md", "Third-party notices"),
    ("LICENSE.txt", "Clip Architect - GNU GPL v3"),
    ("LICENSE-MoneyPrinterTurbo.txt", "MoneyPrinterTurbo - MIT"),
    ("FFMPEG-LICENSE.md", "FFmpeg - which parts are under what"),
    ("FFMPEG-BUILD.txt", "FFmpeg - the build shipped here"),
    ("LGPL-3.0.txt", "GNU Lesser GPL v3"),
    ("LGPL-2.1.txt", "GNU Lesser GPL v2.1"),
    ("OFL-1.1-NotoSans.txt", "Noto Sans - SIL OFL 1.1"),
    ("OFL-1.1-BeVietnamPro.txt", "Be Vietnam Pro - SIL OFL 1.1"),
    ("OFL-1.1-Charm.txt", "Charm - SIL OFL 1.1"),
    ("LICENSES-python.txt", "Python packages"),
];

#[derive(serde::Serialize)]
pub struct LicenseDocument {
    name: String,
    title: String,
}

/// Dev builds read the folder out of the working tree, the same split
/// `backend_command` makes: there is no bundle beside a `tauri dev` binary.
#[cfg(debug_assertions)]
fn licenses_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let _ = app;
    Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or("cannot locate the project root")?
        .join("licenses"))
}

#[cfg(not(debug_assertions))]
fn licenses_dir(app: &AppHandle) -> Result<PathBuf, String> {
    // Scoped to this branch: the debug build resolves the folder from the
    // working tree and never touches the path resolver.
    use tauri::Manager;

    app.path()
        .resource_dir()
        .map(|dir| dir.join("licenses"))
        .map_err(|error| format!("cannot locate the resource directory: {error}"))
}

fn file_names(dir: &PathBuf) -> Result<Vec<String>, String> {
    let mut names: Vec<String> = fs::read_dir(dir)
        .map_err(|error| format!("cannot read {}: {error}", dir.display()))?
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.path().is_file())
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    Ok(names)
}

#[tauri::command]
pub fn license_documents(app: AppHandle) -> Result<Vec<LicenseDocument>, String> {
    let dir = licenses_dir(&app)?;
    let mut present = file_names(&dir)?;

    let mut documents = Vec::with_capacity(present.len());
    for (name, title) in DOCUMENTS {
        if let Some(index) = present.iter().position(|file| file == name) {
            present.remove(index);
            documents.push(LicenseDocument {
                name: (*name).to_string(),
                title: (*title).to_string(),
            });
        }
    }
    documents.extend(present.into_iter().map(|name| LicenseDocument {
        title: name.clone(),
        name,
    }));

    Ok(documents)
}

#[tauri::command]
pub fn read_license_document(app: AppHandle, name: String) -> Result<String, String> {
    let dir = licenses_dir(&app)?;

    // Matched against the directory listing rather than joined onto the path:
    // the name arrives from the webview, and a `..` in it would otherwise turn
    // this into a reader for any file the process can reach.
    if !file_names(&dir)?.iter().any(|file| *file == name) {
        return Err(format!("{name} is not one of the shipped licence files"));
    }

    fs::read_to_string(dir.join(&name)).map_err(|error| format!("cannot read {name}: {error}"))
}
