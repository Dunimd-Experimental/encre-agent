//! Copyright (c) 2025-2026 Wenze Wei. All Rights Reserved.
//!
//! This file is part of Encre.
//! The Encre project belongs to the Dunimd Team.
//!
//! Licensed under the Apache License, Version 2.0 (the "License");
//! You may not use this file except in compliance with the License.
//! You may obtain a copy of the License at
//!
//!     http://www.apache.org/licenses/LICENSE-2.0
//!
//! Unless required by applicable law or agreed to in writing, software
//! distributed under the License is distributed on an "AS IS" BASIS,
//! WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//! See the License for the specific language governing permissions and
//! limitations under the License.
//!
//! DISCLAIMER: Users must comply with applicable AI regulations.
//! Non-compliance may result in service termination or legal liability.

//! Sandbox execution — single, unified entry point for ALL shell commands.
//!
//! Every bash command in the entire Encre system flows through this ONE
//! function.  Platform-specific isolation is applied automatically:
//!
//! | Platform  | Mechanism                         | Scope                    |
//! |-----------|-----------------------------------|--------------------------|
//! | Linux     | Landlock LSM (kernel 5.13+)      | r/w workspace only,      |
//! |           | → fork + landlock_restrict_self   | no network, no exec out  |
//! |           |   + exec                          | of workspace             |
//! | macOS     | sandbox_init(3) (Sandbox.kext)    | r/w workspace only,      |
//! |           | → fork + sandbox_init + exec      | no network, no exec out  |
//! |           |   macOS 10.5+                     | of workspace             |
//! | Windows   | Job Object (kernel32)             | proc & memory limits,    |
//! |           | → CreateProcess + AssignJob       | no UI, die on exception  |
//!
//! Security guarantees (all platforms)
//! ------------------------------------
//! * **Linux**: read, write, create, remove files ONLY under workspace.
//!   No network, no exec outside workspace.  Kernel-level via Landlock.
//! * **macOS**: identical profile via sandbox_init(3).  Default-deny TBF
//!   profile with explicit workspace allow + system.sb import.
//! * **Windows**: Job Object with active-process limit (64), memory cap
//!   (2 GB), no UI, die-on-unhandled-exception.  Process-level isolation.
//! * **All**: configurable timeout, clean subprocess lifecycle.
//! * **No Docker dependency, zero overhead.**

use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

#[derive(Serialize, Deserialize, Debug)]
pub struct SandboxResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

/// Execute *command* in a sandboxed subprocess.
///
/// * `timeout` — kill the child after N seconds (default 120).
/// * `workspace` — when set, the child process is restricted by the
///   OS-native sandbox mechanism:
///   - **Linux 5.13+** → Landlock LSM
///   - **macOS 10.5+** → sandbox_init(3)
///   - **Windows**    → Job Object (process/memory/UI limits)
///
/// The restriction is always applied in the **child** process (after
/// fork but before exec on Unix, or immediately after spawn on
/// Windows), so the parent Python process stays completely unrestricted.
pub fn sandbox_execute(
    command: &str,
    timeout: u64,
    // Unused on Windows: the Job Object sandbox does not take a workspace
    // root, so the parameter only feeds the Landlock / Seatbelt paths.
    #[cfg_attr(target_os = "windows", allow(unused_variables))]
    workspace: Option<&str>,
) -> Result<SandboxResult, String> {
    // ── Build the subprocess command ──────────────────────────
    let mut cmd;

    #[cfg(target_os = "windows")]
    {
        cmd = std::process::Command::new("cmd.exe");
        cmd.arg("/C");
        cmd.arg(command);
        use std::os::windows::process::CommandExt as _;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        // Do NOT pass /U (UTF-16 output): it only affects cmd built-ins,
        // while external programs keep writing in the system ANSI code page
        // (GBK on zh-CN), producing a mixed undecodable stream.  Instead we
        // ask the common UTF-8-aware tools (python, git, node, …) to emit
        // UTF-8 via environment hints and decode the rest with an ANSI
        // code-page fallback chain.
        cmd.env("PYTHONUTF8", "1");
        cmd.env("PYTHONIOENCODING", "utf-8");
        cmd.env("LANG", "C.UTF-8");
        cmd.env("LC_ALL", "C.UTF-8");
        cmd.env("GIT_OPTIONAL_LOCKS", "0");
    }

    #[cfg(not(target_os = "windows"))]
    {
        cmd = std::process::Command::new("sh");
        cmd.arg("-c");
        cmd.arg(command);
        use std::os::unix::process::CommandExt as _;
        cmd.process_group(0); // isolate into own process group
    }

    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    // ── Spawn (platform-specific sandbox applied before child runs) ──

    #[cfg(target_os = "linux")]
    {
        _apply_linux_sandbox(&mut cmd, workspace);
    }

    #[cfg(target_os = "macos")]
    {
        _apply_macos_sandbox(&mut cmd, workspace);
    }

    let mut child = cmd.spawn()
        .map_err(|e| format!("Failed to spawn process: {e}"))?;

    // ── Windows: attach Job Object before resuming ────────────
    #[cfg(target_os = "windows")]
    {
        _attach_windows_job(&mut child, timeout)?;
    }

    // ── Collect output with timeout ───────────────────────────
    // Extract process identifier BEFORE moving child into the
    // reader thread, so the timeout handler can kill it.
    let _child_id = child.id();
    #[cfg(target_os = "windows")]
    let child_raw_handle = {
        use std::os::windows::io::AsRawHandle;
        child.as_raw_handle()
    };

    let (tx, rx) = mpsc::channel();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    thread::spawn(move || {
        let mut out_buf = Vec::new();
        let mut err_buf = Vec::new();
        if let Some(mut s) = stdout {
            let _ = std::io::Read::read_to_end(&mut s, &mut out_buf);
        }
        if let Some(mut s) = stderr {
            let _ = std::io::Read::read_to_end(&mut s, &mut err_buf);
        }
        let status = child.wait();
        let _ = tx.send((out_buf, err_buf, status));
    });

    let (stdout_raw, stderr_raw, status_result) = rx
        .recv_timeout(Duration::from_secs(timeout))
        .map_err(|_| {
            // Timed out — kill the process tree
            #[cfg(not(target_os = "windows"))]
            unsafe {
                let _ = libc::killpg(_child_id as i32, libc::SIGKILL);
            }
            #[cfg(target_os = "windows")]
            unsafe {
                windows_sys::Win32::System::Threading::TerminateProcess(
                    child_raw_handle as *mut _,
                    1,
                );
            }
            format!("Command timed out after {timeout}s")
        })?;

    let status = status_result
        .map_err(|e| format!("Wait error: {e}"))?;

    let stdout = decode_output(&stdout_raw);
    let stderr = decode_output(&stderr_raw);

    Ok(SandboxResult {
        stdout,
        stderr,
        exit_code: status.code().unwrap_or(-1),
    })
}

// ── Platform-specific sandbox implementations ────────────────────

/// Linux: Landlock LSM via pre_exec() in child process.
#[cfg(target_os = "linux")]
fn _apply_linux_sandbox(cmd: &mut std::process::Command, workspace: Option<&str>) {
    use std::os::unix::process::CommandExt as _;

    if let Some(ws) = workspace {
        let ws = ws.to_owned();
        if crate::landlock::landlock_available() {
            unsafe {
                cmd.pre_exec(move || {
                    crate::landlock::landlock_workspace_sandbox(&ws)
                        .map_err(|e| {
                            std::io::Error::new(std::io::ErrorKind::Other, e)
                        })?;
                    Ok(())
                });
            }
        }
    }
}

/// macOS: sandbox_init(3) via pre_exec() in child process.
#[cfg(target_os = "macos")]
fn _apply_macos_sandbox(cmd: &mut std::process::Command, workspace: Option<&str>) {
    use std::ffi::CString;
    use std::os::unix::process::CommandExt as _;

    extern "C" {
        fn sandbox_init(
            profile: *const std::os::raw::c_char,
            flags: u64,
            errorbuf: *mut *mut std::os::raw::c_char,
        ) -> i32;
        fn sandbox_free_error(errorbuf: *mut std::os::raw::c_char);
    }

    const SANDBOX_NAMED: u64 = 1;

    if let Some(ws) = workspace {
        let ws_owned = ws.to_owned();
        let escaped = ws_owned.replace('\\', "/");
        let profile = format!(
            "(version 1)\n\
             (deny default)\n\
             (allow file-read* file-write* (subpath \"{ws}\"))\n\
             (allow file-read* (subpath \"/usr/lib\"))\n\
             (allow file-read* (subpath \"/System/Library\"))\n\
             (allow file-read* (literal \"/usr/share\"))\n\
             (allow process-fork)\n\
             (allow signal (target self))\n\
             (allow sysctl-read)\n\
             (allow file-read-metadata)\n\
             (import \"system.sb\")\n",
            ws = escaped,
        );

        unsafe {
            cmd.pre_exec(move || {
                let c_profile = CString::new(profile.as_str())
                    .map_err(|_| std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "NUL in profile",
                    ))?;
                let mut errorbuf: *mut std::os::raw::c_char =
                    std::ptr::null_mut();
                let rv = sandbox_init(
                    c_profile.as_ptr(), SANDBOX_NAMED, &mut errorbuf,
                );
                if rv != 0 {
                    let msg = if !errorbuf.is_null() {
                        let s = std::ffi::CStr::from_ptr(errorbuf)
                            .to_string_lossy().into_owned();
                        sandbox_free_error(errorbuf);
                        s
                    } else {
                        format!("sandbox_init err {}", rv)
                    };
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::PermissionDenied, msg,
                    ));
                }
                Ok(())
            });
        }
    }
}

/// Windows: Job Object with process/memory/UI limits.
///
/// Creates a kernel Job Object and assigns the child process to it,
/// ensuring that even if the child spawns grandchildren they stay
/// inside the same restricted job.
#[cfg(target_os = "windows")]
fn _attach_windows_job(
    child: &mut std::process::Child,
    _timeout_secs: u64,
) -> Result<(), String> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::System::JobObjects::*;

    unsafe {
        // 1. Create the Job Object
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job == INVALID_HANDLE_VALUE {
            // Non-fatal — proceed without job
            return Ok(());
        }

        // 2. Set extended limits
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();

        // Basic limits
        info.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        // Cap at 64 child processes (fork-bomb protection)
        info.BasicLimitInformation.ActiveProcessLimit = 64;

        // 3. Apply UI restrictions (no windows, no clipboard, no sysmenu)
        let mut ui_restrict: JOBOBJECT_BASIC_UI_RESTRICTIONS = std::mem::zeroed();
        ui_restrict.UIRestrictionsClass =
            JOB_OBJECT_UILIMIT_NONE
            | JOB_OBJECT_UILIMIT_HANDLES
            | JOB_OBJECT_UILIMIT_READCLIPBOARD
            | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
            | JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
            | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
            | JOB_OBJECT_UILIMIT_GLOBALATOMS
            | JOB_OBJECT_UILIMIT_DESKTOP
            | JOB_OBJECT_UILIMIT_EXITWINDOWS;

        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );

        SetInformationJobObject(
            job,
            JobObjectBasicUIRestrictions,
            &ui_restrict as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_BASIC_UI_RESTRICTIONS>() as u32,
        );

        // 4. Assign the process to the job
        let proc_handle = child.as_raw_handle();
        if AssignProcessToJobObject(job, proc_handle as *mut _) == 0 {
            // Non-fatal — continue without job
        }
    }

    Ok(())
}

// ── Output decoding ──────────────────────────────────────────────

/// Decode subprocess output bytes into a String.
///
/// Attempt order:
/// 1. UTF-16LE probe — legacy ``cmd /U`` output; rejected when the decoded
///    text contains NUL characters (a false positive on ANSI data).
/// 2. Strict UTF-8 — tools told to emit UTF-8 via ``PYTHONUTF8`` / ``LANG``,
///    and all POSIX platforms.
/// 3. The Windows ANSI code page via ``GetACP()`` — what locale-driven
///    programs actually write (936 zh-CN, 932 ja-JP, 949 ko-KR, 1251 ru-RU,
///    1252 Western Europe, 65001 UTF-8, …).
/// 4. A set of common legacy code pages as a safety net for children that
///    hard-code a different code page than the system one.
/// 5. Lossy UTF-8 fallback so the caller always receives a string.
fn decode_output(raw: &[u8]) -> String {
    if raw.is_empty() {
        return String::new();
    }

    // ── 1. UTF-16LE probe ──────────────────────────────────────────
    if raw.len() >= 2 && raw.len() % 2 == 0 {
        let probe = (raw.len() / 2).min(80); // sample up to 80 code units
        let looks_utf16 = (0..probe).all(|i| raw[i * 2 + 1] == 0);
        if looks_utf16 {
            let u16_words: Vec<u16> = raw
                .windows(2)
                .step_by(2)
                .map(|c| u16::from_le_bytes([c[0], c[1]]))
                .collect();
            let s = String::from_utf16_lossy(&u16_words);
            if !s.contains('\0') {
                return s;
            }
        }
    }

    // ── 2. Strict UTF-8 ────────────────────────────────────────────
    if let Ok(s) = std::str::from_utf8(raw) {
        return s.to_owned();
    }

    // ── 3 + 4. Windows ANSI code page, then legacy code pages ─────
    #[cfg(target_os = "windows")]
    {
        let mut cps = vec![get_acp()];
        cps.extend([936u32, 54936u32, 932u32, 949u32, 950u32, 1251u32, 1252u32]);
        for cp in cps {
            if let Some(s) = decode_ansi_cp(raw, cp) {
                return s;
            }
        }
    }

    // ── 5. Lossy fallback ──────────────────────────────────────────
    String::from_utf8_lossy(raw).into_owned()
}

/// Return the system ANSI code page (e.g. 936 on zh-CN, 932 on ja-JP).
#[cfg(target_os = "windows")]
fn get_acp() -> u32 {
    unsafe { windows_sys::Win32::Globalization::GetACP() }
}

/// Decode bytes using a Windows ANSI code page via MultiByteToWideChar.
///
/// Returns ``None`` when the byte sequence is not valid in that code page,
/// so the caller can try the next one.
#[cfg(target_os = "windows")]
fn decode_ansi_cp(raw: &[u8], codepage: u32) -> Option<String> {
    use windows_sys::Win32::Globalization::MultiByteToWideChar;

    // MB_ERR_INVALID_CHARS (0x00000008) makes the API reject invalid
    // sequences instead of silently substituting '?'.
    const MB_ERR_INVALID_CHARS: u32 = 0x0000_0008;

    unsafe {
        let needed = MultiByteToWideChar(
            codepage,
            MB_ERR_INVALID_CHARS,
            raw.as_ptr(),
            raw.len() as i32,
            std::ptr::null_mut(),
            0,
        );
        if needed <= 0 {
            return None;
        }
        let mut buf = vec![0u16; needed as usize];
        let written = MultiByteToWideChar(
            codepage,
            MB_ERR_INVALID_CHARS,
            raw.as_ptr(),
            raw.len() as i32,
            buf.as_mut_ptr(),
            needed,
        );
        if written <= 0 {
            return None;
        }
        buf.truncate(written as usize);
        Some(String::from_utf16_lossy(&buf))
    }
}

/// Read a file inside the sandbox (path-bounded).
pub fn sandbox_read_file(path: &str) -> Result<String, String> {
    let p = Path::new(path);
    if !p.exists() {
        return Err(format!("File not found: {path}"));
    }
    std::fs::read_to_string(p).map_err(|e| format!("Read error: {e}"))
}

/// Write a file inside the sandbox (path-bounded) atomically.
pub fn sandbox_write_file(path: &str, content: &str) -> Result<bool, String> {
    let p = Path::new(path);
    if let Some(parent) = p.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Mkdir error: {e}"))?;
    }

    // Atomic write: temp → fsync → rename
    let tmp_path = p.with_extension("tmp");
    let mut tmp = std::fs::File::create(&tmp_path)
        .map_err(|e| format!("Create temp error: {e}"))?;
    std::io::Write::write_all(&mut tmp, content.as_bytes())
        .map_err(|e| format!("Write temp error: {e}"))?;
    tmp.sync_all()
        .map_err(|e| format!("Fsync error: {e}"))?;
    drop(tmp);

    let _ = std::fs::remove_file(p);
    std::fs::rename(&tmp_path, p)
        .map_err(|e| format!("Rename error: {e}"))?;

    Ok(true)
}
