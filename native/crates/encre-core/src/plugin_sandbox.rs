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

//! Plugin subprocess sandbox policy.
//!
//! Everything security-relevant about spawning a plugin's worker process
//! is decided here, in Rust, on every platform (Windows / Linux / macOS):
//!
//! * **Environment sanitisation** — sensitive variables (tokens, keys,
//!   secrets, credentials, passwords) never cross the plugin boundary.
//! * **Resource-limit resolution** — the manifest's declared limits
//!   (``memory_mb``, ``cpu``) are validated and translated into the
//!   concrete per-OS values the transports apply (Windows Job Object
//!   memory cap + CPU rate, POSIX ``RLIMIT_AS``).

use std::collections::HashMap;

/// Substrings that mark an environment variable as sensitive.  Any variable
/// whose upper-cased name contains one of these is stripped from the child
/// environment to prevent credential leakage.
const ENV_SENSITIVE: [&str; 6] = [
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "API_KEY",
];

/// Build the sanitised child-process environment.
///
/// The parent's environment is copied with every sensitive variable removed,
/// then *extra* entries (plugin factory target, PYTHONPATH, ...) are applied
/// last and unconditionally — they are host-generated, never user secrets.
pub fn plugin_sandbox_prepare_env(extra: HashMap<String, String>) -> HashMap<String, String> {
    let mut env: HashMap<String, String> = std::env::vars()
        .filter(|(key, _)| {
            let upper = key.to_uppercase();
            !ENV_SENSITIVE.iter().any(|s| upper.contains(s))
        })
        .collect();
    for (key, value) in extra {
        env.insert(key, value);
    }
    env
}

/// Resolved, per-platform resource limits for a plugin worker process.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ResolvedLimits {
    /// Hard cap on the child's address space in bytes; ``0`` = unlimited.
    /// Applied as the Windows Job `PROCESS_MEMORY` limit and as POSIX
    /// ``RLIMIT_AS``.
    pub memory_bytes: u64,
    /// Windows Job CPU rate (1..=10000, fraction of total CPU); ``0`` =
    /// unlimited.  Derived from ``cpu`` (core fraction) against the machine's
    /// parallelism.
    pub cpu_rate: u64,
    /// Normalised CPU core fraction; ``0.0`` = unlimited.  Consumed by the
    /// software watchdog on platforms without a hard CPU control.
    pub cpu_fraction: f64,
}

/// Validate and resolve the manifest's declared resource limits.
///
/// Non-positive or NaN inputs mean "no limit" rather than "clamp to zero":
/// a malformed manifest must never produce a degenerate sandbox (e.g. a
/// zero-byte memory cap that kills the worker instantly), it just degrades
/// to the watchdog-only layer.
pub fn plugin_sandbox_resolve_limits(memory_mb: Option<f64>, cpu: Option<f64>) -> ResolvedLimits {
    let memory_bytes = match memory_mb {
        Some(mb) if mb.is_finite() && mb > 0.0 => (mb * 1024.0 * 1024.0) as u64,
        _ => 0,
    };
    let cores = std::thread::available_parallelism()
        .map(|n| n.get() as f64)
        .unwrap_or(1.0);
    let cpu_fraction = match cpu {
        Some(c) if c.is_finite() && c > 0.0 => c.clamp(0.01, cores),
        _ => 0.0,
    };
    let cpu_rate = if cpu_fraction > 0.0 {
        ((cpu_fraction / cores) * 10000.0).round().clamp(1.0, 10000.0) as u64
    } else {
        0
    };
    ResolvedLimits {
        memory_bytes,
        cpu_rate,
        cpu_fraction,
    }
}
