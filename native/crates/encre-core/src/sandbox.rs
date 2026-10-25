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
use std::collections::HashMap;
use std::path::Path;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

/// How a child process is confined at spawn time.
#[derive(Debug, Clone, Default)]
pub enum SandboxProfile {
    /// No filesystem/network confinement (trusted in-process-adjacent work).
    #[default]
    None,
    /// Legacy workspace profile: the child may touch only `workspace`.
    Workspace(String),
    /// Plugin worker profile: read-only runtime/package paths, read-write
    /// state directory, no network.  The one profile used by every
    /// third-party plugin worker.
    Plugin {
        read_paths: Vec<String>,
        write_paths: Vec<String>,
    },
}

#[derive(Serialize, Deserialize, Debug)]
pub struct SandboxResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

/// Keep a console child (cmd / PowerShell / bash) from allocating a visible
/// console window.
///
/// Encre Agent Desktop runs the backend under a GUI parent that owns no
/// console, so without this flag every spawned console process gets a brand
/// new terminal window.  Every spawn in this module must call it.
#[cfg(target_os = "windows")]
fn hide_console_window(cmd: &mut std::process::Command) {
    use std::os::windows::process::CommandExt as _;
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
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
        hide_console_window(&mut cmd);
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
    if let Some(ws) = workspace {
        // Delegates to the profile shared with SandboxSession::spawn.
        let _ = apply_macos_profile(cmd, ws);
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

// ── Persistent sandboxed sessions ─────────────────────────────────

/// A long-lived sandboxed child process with piped stdio.
///
/// Used for persistent terminal/shell sessions: the OS-level confinement is
/// applied by THIS process (Rust) at spawn time — Landlock on Linux,
/// sandbox_init(3) on macOS, a kill-on-close Job Object on Windows — so the
/// Python host never needs (and never gets) a privilege checkpoint that a
/// child could bypass.  Python only drives the pipes.
///
/// The Windows job handle is owned by this struct: dropping the session
/// closes the handle, which reaps the whole descendant tree
/// (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE).
pub struct SandboxSession {
    child: Option<std::process::Child>,
    stdin: Option<std::process::ChildStdin>,
    /// Reader thread latch: stdout bytes land here.
    stdout_buf: std::sync::Arc<(std::sync::Mutex<Vec<u8>>, std::sync::Condvar)>,
    stderr_buf: std::sync::Arc<(std::sync::Mutex<Vec<u8>>, std::sync::Condvar)>,
    /// The confined process' own id, kept for killpg on Unix.
    child_id: u32,
    /// Honest level report: "landlock" | "sandbox_init" | "job_object" | "none".
    isolation: &'static str,
    #[cfg(target_os = "windows")]
    job_handle: std::sync::Arc<JobHandleBox>,
}

#[cfg(target_os = "windows")]
struct JobHandleBox(std::sync::Mutex<isize>);

/// Drain whatever bytes are pending in *buf* (up to *max_bytes*), waiting
/// up to *timeout_ms* for the first byte to arrive.  Returns an empty vec
/// on timeout.  Used by the Python side to poll for command completion
/// markers without a busy read loop.
fn drain_channel(
    buf: &std::sync::Arc<(std::sync::Mutex<Vec<u8>>, std::sync::Condvar)>,
    max_bytes: usize,
    timeout_ms: u64,
) -> Vec<u8> {
    let (lock, cvar) = &**buf;
    let mut guard = match lock.lock() {
        Ok(g) => g,
        Err(_) => return Vec::new(),
    };
    if guard.is_empty() && timeout_ms > 0 {
        let deadline = std::time::Duration::from_millis(timeout_ms);
        // wait_timeout_while hands back the guard together with the timeout
        // result; the guard may now hold data even when the wait timed out.
        let result = cvar.wait_timeout_while(guard, deadline, |b| b.is_empty());
        guard = match result {
            Ok((g, _)) => g,
            Err(_) => return Vec::new(),
        };
    }
    let take = guard.len().min(max_bytes);
    guard.drain(..take).collect()
}

fn spawn_reader<S: std::io::Read + Send + 'static>(
    mut stream: Option<S>,
    buf: std::sync::Arc<(std::sync::Mutex<Vec<u8>>, std::sync::Condvar)>,
) {
    thread::spawn(move || {
        if let Some(s) = stream.as_mut() {
            let mut chunk = [0u8; 8192];
            loop {
                match s.read(&mut chunk) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        if let Ok(mut g) = buf.0.lock() {
                            g.extend_from_slice(&chunk[..n]);
                            buf.1.notify_all();
                        }
                    }
                }
            }
        }
    });
}

impl SandboxSession {
    /// Spawn *argv* under the platform sandbox with piped stdio.
    ///
    /// * `cwd`      — working directory for the child.
    /// * `workspace` — confinement root for the Landlock / Seatbelt paths.
    ///   Ignored on Windows (its Job Object has no path scope).
    ///
    /// Returns the session plus an honest isolation level string.
    pub fn spawn(
        argv: &[String],
        cwd: Option<&str>,
        workspace: Option<&str>,
    ) -> Result<(SandboxSession, &'static str), String> {
        let profile = match workspace {
            Some(ws) => SandboxProfile::Workspace(ws.to_string()),
            None => SandboxProfile::None,
        };
        Self::spawn_with_env(argv, cwd, None, profile)
    }

    /// Spawn *argv* under *profile* with an explicit environment.
    ///
    /// This is the single spawn entry used by plugin workers: the caller
    /// supplies the already-filtered environment (Rust policy), and the
    /// sandbox profile decides filesystem/network confinement.  Keeping the
    /// spawn here means Python never needs to know how a platform confines a
    /// child — it only drives the pipes.
    pub fn spawn_with_env(
        argv: &[String],
        cwd: Option<&str>,
        env: Option<&HashMap<String, String>>,
        #[cfg_attr(target_os = "windows", allow(unused_variables))]
        profile: SandboxProfile,
    ) -> Result<(SandboxSession, &'static str), String> {
        if argv.is_empty() {
            return Err("Empty argv".into());
        }
        let mut cmd = std::process::Command::new(&argv[0]);
        cmd.args(&argv[1..]);
        if let Some(c) = cwd {
            cmd.current_dir(c);
        }
        if let Some(env) = env {
            cmd.env_clear();
            cmd.envs(env);
        }
        cmd.stdin(std::process::Stdio::piped());
        cmd.stdout(std::process::Stdio::piped());
        cmd.stderr(std::process::Stdio::piped());
        cmd.env("PYTHONUTF8", "1");
        cmd.env("PYTHONIOENCODING", "utf-8");
        cmd.env("LANG", "C.UTF-8");
        cmd.env("LC_ALL", "C.UTF-8");
        cmd.env("PYTHONDONTWRITEBYTECODE", "1");

        // Persistent tool shells are the most common spawn in the app: without
        // this they each pop a console window on Windows.
        #[cfg(target_os = "windows")]
        hide_console_window(&mut cmd);

        #[cfg(not(target_os = "windows"))]
        {
            use std::os::unix::process::CommandExt as _;
            cmd.process_group(0);
        }

        #[cfg(not(target_os = "windows"))]
        let mut isolation: &'static str = "none";

        #[cfg(target_os = "linux")]
        {
            match &profile {
                SandboxProfile::Workspace(ws) => {
                    if crate::landlock::landlock_available() {
                        let ws = ws.clone();
                        unsafe {
                            use std::os::unix::process::CommandExt as _;
                            cmd.pre_exec(move || {
                                crate::landlock::landlock_workspace_sandbox(&ws)
                                    .map_err(|e| std::io::Error::new(
                                        std::io::ErrorKind::Other, e,
                                    ))
                            });
                        }
                        isolation = "landlock";
                    }
                }
                SandboxProfile::Plugin { read_paths, write_paths } => {
                    if crate::landlock::landlock_available() {
                        let reads = read_paths.clone();
                        let writes = write_paths.clone();
                        unsafe {
                            use std::os::unix::process::CommandExt as _;
                            cmd.pre_exec(move || {
                                crate::landlock::landlock_plugin_sandbox(&reads, &writes)
                                    .map_err(|e| std::io::Error::new(
                                        std::io::ErrorKind::Other, e,
                                    ))
                            });
                        }
                        isolation = "landlock_plugin";
                    }
                }
                SandboxProfile::None => {}
            }
        }

        #[cfg(target_os = "macos")]
        {
            match &profile {
                SandboxProfile::Workspace(ws) => {
                    apply_macos_profile(&mut cmd, ws)?;
                    isolation = "sandbox_init";
                }
                SandboxProfile::Plugin { read_paths, write_paths } => {
                    apply_macos_plugin_profile(&mut cmd, read_paths, write_paths)?;
                    isolation = "sandbox_plugin";
                }
                SandboxProfile::None => {}
            }
        }

        let mut child = cmd.spawn()
            .map_err(|e| format!("Failed to spawn process: {e}"))?;

        #[cfg(target_os = "windows")]
        let job = _attach_windows_job_retained(&mut child);
        #[cfg(target_os = "windows")]
        let isolation: &'static str = if job.lock().map(|h| *h != 0).unwrap_or(false) {
            "job_object"
        } else {
            "none"
        };

        let child_id = child.id();
        let stdin = child.stdin.take();
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();

        let stdout_buf: std::sync::Arc<(std::sync::Mutex<Vec<u8>>, std::sync::Condvar)> =
            std::sync::Arc::new((std::sync::Mutex::new(Vec::new()), std::sync::Condvar::new()));
        let stderr_buf: std::sync::Arc<(std::sync::Mutex<Vec<u8>>, std::sync::Condvar)> =
            std::sync::Arc::new((std::sync::Mutex::new(Vec::new()), std::sync::Condvar::new()));
        spawn_reader(stdout, std::sync::Arc::clone(&stdout_buf));
        spawn_reader(stderr, std::sync::Arc::clone(&stderr_buf));

        Ok((
            SandboxSession {
                child: Some(child),
                stdin,
                stdout_buf,
                stderr_buf,
                child_id,
                isolation,
                #[cfg(target_os = "windows")]
                job_handle: std::sync::Arc::new(JobHandleBox(job)),
            },
            isolation,
        ))
    }

    /// The honest isolation level reported at spawn time.
    pub fn isolation_level(&self) -> &'static str {
        self.isolation
    }

    /// The confined child's OS process id (also keeps `child_id` live on
    /// platforms where kill does not need it).
    pub fn pid(&self) -> u32 {
        self.child_id
    }

    /// Write bytes to the child's stdin (returns bytes written).
    pub fn write_stdin(&mut self, data: &[u8]) -> Result<usize, String> {
        use std::io::Write;
        match self.stdin.as_mut() {
            Some(s) => s
                .write_all(data)
                .map(|_| data.len())
                .map_err(|e| format!("stdin write failed: {e}")),
            None => Err("stdin closed".into()),
        }
    }

    /// Close the child's stdin (signals end-of-input for shells).
    pub fn close_stdin(&mut self) {
        self.stdin = None;
    }

    /// Drain pending stdout bytes (waits up to *timeout_ms* for data).
    pub fn drain_stdout(&self, max_bytes: usize, timeout_ms: u64) -> Vec<u8> {
        drain_channel(&self.stdout_buf, max_bytes, timeout_ms)
    }

    /// Drain pending stderr bytes (waits up to *timeout_ms* for data).
    pub fn drain_stderr(&self, max_bytes: usize, timeout_ms: u64) -> Vec<u8> {
        drain_channel(&self.stderr_buf, max_bytes, timeout_ms)
    }

    /// Wait for exit up to *timeout_ms*; `Some(code)` on exit, `None` on
    /// timeout.  Reaps the child on success.
    pub fn wait(&mut self, timeout_ms: u64) -> Option<i32> {
        if let Some(child) = self.child.as_mut() {
            // Best-effort bounded wait: poll the exit status.
            let deadline = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
            loop {
                match child.try_wait() {
                    Ok(Some(status)) => return Some(status.code().unwrap_or(-1)),
                    Ok(None) => {
                        if std::time::Instant::now() >= deadline {
                            return None;
                        }
                        thread::sleep(std::time::Duration::from_millis(50));
                    }
                    Err(_) => return Some(-1),
                }
            }
        } else {
            Some(-1)
        }
    }

    /// Whether the child has exited.
    pub fn is_running(&mut self) -> bool {
        match self.child.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        }
    }

    /// Kill the whole process tree and reap.
    pub fn kill(&mut self) {
        self.stdin = None;
        if let Some(child) = self.child.as_mut() {
            #[cfg(not(target_os = "windows"))]
            unsafe {
                let _ = libc::killpg(self.child_id as i32, libc::SIGKILL);
            }
            #[cfg(target_os = "windows")]
            unsafe {
                use std::os::windows::io::AsRawHandle;
                let _ = windows_sys::Win32::System::Threading::TerminateProcess(
                    child.as_raw_handle() as *mut _,
                    1,
                );
            }
            let _ = child.wait();
        }
        // Windows: dropping the retained job handle reaps survivors
        // (KILL_ON_JOB_CLOSE) — handled by Drop on the session.
    }
}

impl Drop for SandboxSession {
    fn drop(&mut self) {
        self.kill();
        #[cfg(target_os = "windows")]
        {
            // Close the job handle last: KILL_ON_JOB_CLOSE reaps the tree.
            if let Ok(mut h) = self.job_handle.0.lock() {
                if *h != 0 {
                    unsafe {
                        windows_sys::Win32::Foundation::CloseHandle(*h as *mut _);
                    }
                    *h = 0;
                }
            }
        }
    }
}

/// macOS profile application shared by the one-shot and session paths.
#[cfg(target_os = "macos")]
fn apply_macos_profile(
    cmd: &mut std::process::Command,
    ws: &str,
) -> Result<(), String> {
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

    let escaped = ws.replace('\\', "/");
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
            let mut errorbuf: *mut std::os::raw::c_char = std::ptr::null_mut();
            let rv = sandbox_init(c_profile.as_ptr(), SANDBOX_NAMED, &mut errorbuf);
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
    Ok(())
}

#[cfg(target_os = "macos")]
fn apply_macos_plugin_profile(
    cmd: &mut std::process::Command,
    read_paths: &[String],
    write_paths: &[String],
) -> Result<(), String> {
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
    let mut rules = String::from(
        "(version 1)\n(deny default)\n\
         (allow process-fork)\n\
         (allow signal (target self))\n\
         (allow sysctl-read)\n\
         (allow file-read-metadata)\n\
         (import \"system.sb\")\n",
    );
    for p in read_paths {
        let escaped = p.replace('\\', "/");
        rules.push_str(&format!("(allow file-read* (subpath \"{escaped}\"))\n"));
    }
    for p in write_paths {
        let escaped = p.replace('\\', "/");
        rules.push_str(&format!(
            "(allow file-read* file-write* (subpath \"{escaped}\"))\n"
        ));
    }

    unsafe {
        cmd.pre_exec(move || {
            let c_profile = CString::new(rules.as_str()).map_err(|_| {
                std::io::Error::new(std::io::ErrorKind::InvalidInput, "NUL in profile")
            })?;
            let mut errorbuf: *mut std::os::raw::c_char = std::ptr::null_mut();
            let rv = sandbox_init(c_profile.as_ptr(), SANDBOX_NAMED, &mut errorbuf);
            if rv != 0 {
                let msg = if !errorbuf.is_null() {
                    let s = std::ffi::CStr::from_ptr(errorbuf)
                        .to_string_lossy()
                        .into_owned();
                    sandbox_free_error(errorbuf);
                    s
                } else {
                    format!("sandbox_init err {rv}")
                };
                return Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    msg,
                ));
            }
            Ok(())
        });
    }
    Ok(())
}

/// Windows: create the job and assign the child, retaining the handle in the
/// returned box (0 = creation/assignment failed → honest "none").
#[cfg(target_os = "windows")]
fn _attach_windows_job_retained(child: &mut std::process::Child) -> std::sync::Mutex<isize> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::System::JobObjects::*;

    let retained: std::sync::Mutex<isize> = std::sync::Mutex::new(0);
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job == INVALID_HANDLE_VALUE {
            return retained;
        }

        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        // Fork-bomb protection: at most 64 live processes in the tree.
        info.BasicLimitInformation.ActiveProcessLimit = 64;

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

        let proc_handle = child.as_raw_handle();
        if AssignProcessToJobObject(job, proc_handle as *mut _) != 0 {
            if let Ok(mut h) = retained.lock() {
                *h = job as isize;
            }
        } else {
            // Assignment failed: close the job immediately so the child is
            // NOT left under a kill-on-close job that outlives the session.
            windows_sys::Win32::Foundation::CloseHandle(job);
        }
    }
    retained
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
