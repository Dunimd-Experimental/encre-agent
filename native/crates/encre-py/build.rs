// Copyright (c) 2025-2026 Wenze Wei. All Rights Reserved.
//
// This file is part of Encre.
// The Encre project belongs to the Dunimd Team.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
//! Build script for the `encre-py` Python extension module.
//!
//! `encre-py` builds a `cdylib` (`_native`) that imports the CPython C API.
//! Those symbols must stay undefined in the library and be resolved at load
//! time from the interpreter that imports the module — the module must not
//! link `libpython` itself. Linux and Windows resolve the leftover symbols
//! on their own, but the macOS linker refuses unless it is explicitly told
//! to defer them. PyO3 0.24 does not set this up automatically: its own
//! build script only emits the framework link args, and link arguments are
//! not propagated from a dependency's build script. The extension crate has
//! to emit them, which is what happens below.

fn main() {
    println!("cargo:rerun-if-changed=build.rs");

    // Read the target OS from Cargo rather than a compile-time `cfg`, so the
    // behaviour is also correct when cross-compiling (the build script runs
    // on the host, but the flags apply to the target link step).
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        // `-undefined dynamic_lookup` defers the CPython symbols to dyld.
        // `cargo:rustc-cdylib-link-arg` only applies to `cdylib` targets.
        println!("cargo:rustc-cdylib-link-arg=-undefined");
        println!("cargo:rustc-cdylib-link-arg=dynamic_lookup");
    }
}
