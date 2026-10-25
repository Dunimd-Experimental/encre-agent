//! Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
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

//! Filesystem read/write helpers exposed to the Encre tool layer.
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

/// Read a slice of a file by 1-based `offset` and `limit` line count.
///
/// Fast path (offset=0, limit=0): reads the entire file via `read_to_string`.
/// Streaming path (pagination): iterates lines with `BufReader` so only the
/// requested range is held in memory — avoids loading huge files entirely.
pub fn read_file(path: &str, offset: usize, limit: usize) -> Result<String, String> {
    let p = Path::new(path);
    if !p.exists() {
        return Err(format!("File not found: {}", path));
    }

    let start_line = if offset > 0 { offset - 1 } else { 0 };

    // Fast path: full file read — single allocation.
    if start_line == 0 && limit == 0 {
        return std::fs::read_to_string(p).map_err(|e| format!("Error reading file: {}", e));
    }

    // Streaming path: only iterate lines in the requested range.
    let file = File::open(p).map_err(|e| format!("Error opening file: {}", e))?;
    let reader = BufReader::new(file);
    let end_line = if limit > 0 {
        start_line.saturating_add(limit)
    } else {
        usize::MAX
    };

    let mut result = String::new();
    let mut first = true;
    for (i, line_result) in reader.lines().enumerate() {
        if i >= end_line {
            break;
        }
        if i < start_line {
            continue;
        }
        let line = line_result.map_err(|e| format!("Error reading line: {}", e))?;
        if !first {
            result.push('\n');
        }
        result.push_str(&line);
        first = false;
    }

    Ok(result)
}

/// Write `content` to `path` atomically.
///
/// Writes to a temporary file first, calls `sync_all` to flush OS caches,
/// then renames over the target — preventing corruption on crash/power-loss.
pub fn write_file(path: &str, content: &str) -> Result<bool, String> {
    let p = Path::new(path);
    if let Some(parent) = p.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Error creating directories: {}", e))?;
    }

    // Atomic write: temp → fsync → rename
    let tmp_path = p.with_extension("tmp");
    let mut tmp = std::fs::File::create(&tmp_path)
        .map_err(|e| format!("Error creating temp file: {}", e))?;
    std::io::Write::write_all(&mut tmp, content.as_bytes())
        .map_err(|e| format!("Error writing temp file: {}", e))?;
    tmp.sync_all()
        .map_err(|e| format!("Error flushing temp file: {}", e))?;
    drop(tmp);

    // Remove destination first (Windows rename fails if target exists)
    let _ = std::fs::remove_file(p);
    std::fs::rename(&tmp_path, p)
        .map_err(|e| format!("Error moving file into place: {}", e))?;

    Ok(true)
}
