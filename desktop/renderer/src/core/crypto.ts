/**
 * Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
 *
 * This file is part of Encre.
 * The Encre project belongs to the Dunimd Team.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 * 
 * DISCLAIMER: Users must comply with applicable AI regulations.
 * Non-compliance may result in service termination or legal liability.
 */

/**
 * Encre cryptographic layer (TypeScript / Web Crypto API).
 *
 * Uses the same AES‑256‑GCM algorithm and keyfile format as the Python
 * `encre/crypto.py` module.  The master key is read (and unwrapped) once
 * at startup via the Electron preload bridge.
 *
 * All transport encryption is done through the raw-bytes path:
 *
 *   encryptRaw(plainBytes) → nonce(12) || ciphertext || tag(16)
 *   decryptRaw(packed)     → plainBytes
 *
 * Ciphertexts are then base64-encoded before being sent over WebSocket.
 *
 * Every frame in both directions is encrypted — including per-token deltas,
 * keepalives and error frames. There is no plaintext fallback anywhere in
 * this module: `encrypt`/`decrypt` throw if the master key is unavailable,
 * and the backend closes any connection whose first frame is not ciphertext.
 */

const KEY_LENGTH = 32;       // AES-256
const NONCE_LENGTH = 12;     // GCM standard
const TAG_LENGTH = 16;       // GCM 128-bit auth tag

let _masterKey: CryptoKey | null = null;
let _ready = false;
// Throttles init retries: the keyfile is written by the backend, so a window
// that connects before it exists must not re-read the disk on every message.
let _lastInitAttempt = 0;
const INIT_RETRY_INTERVAL_MS = 2_000;

// ── Machine identity for key wrapping (scheme v3) ────────────────
// MUST match harness/encre/crypto.py exactly: HKDF-SHA256 over an IKM made of
// the length-prefixed multi-source machine fingerprint and the vendor
// application secret. The fingerprint itself is assembled in the main
// process (registry/files are not reachable from the renderer).

const HKDF_SALT = new TextEncoder().encode("encre-crypto-hkdf-v3");
const HKDF_INFO = new TextEncoder().encode("encre-machine-binding-wrapping-key-v3");
const WRAP_CTX_AAD = new TextEncoder().encode("encre-keywrap-v1");
const KEYFILE_VERSION = 0x03;

/** HMAC-SHA256 over WebCrypto (used to build RFC 5869 HKDF by hand). */
async function hmacSha256(keyBytes: Uint8Array, data: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  const key = await crypto.subtle.importKey(
    "raw",
    keyBytes.buffer as ArrayBuffer,
    { name: "HMAC", hash: "SHA-256" } as HmacImportParams,
    false,
    ["sign"] as KeyUsage[]
  );
  const sig = await crypto.subtle.sign("HMAC", key, data.buffer as ArrayBuffer);
  return new Uint8Array(sig);
}

/** RFC 5869 Expand step. */
async function hkdfExpand(prk: Uint8Array, info: Uint8Array, length: number): Promise<Uint8Array> {
  const okm = new Uint8Array(length);
  let block = new Uint8Array(0);
  let offset = 0;
  let counter = 1;
  while (offset < length) {
    const input = new Uint8Array(block.byteLength + info.byteLength + 1);
    input.set(block, 0);
    input.set(info, block.byteLength);
    input[input.byteLength - 1] = counter++;
    block = await hmacSha256(prk, input);
    const n = Math.min(block.byteLength, length - offset);
    okm.set(block.subarray(0, n), offset);
    offset += n;
  }
  return okm;
}

/** Derives the raw AES-GCM wrapping key (scheme v3, matches Python _derive_wrapping_key). */
async function deriveWrappingKeyRaw(): Promise<Uint8Array> {
  const mat = await window.electronAPI?.readKeyWrapMaterial();
  if (!mat || !mat.fingerprint) {
    throw new Error("Unable to determine machine identity for key wrapping");
  }
  const machine = new TextEncoder().encode(mat.fingerprint);
  const secret = new TextEncoder().encode(mat.appSecret);

  // IKM = len4(machine) || machine || len4(secret) || secret (big-endian lengths)
  const ikm = new Uint8Array(8 + machine.byteLength + secret.byteLength);
  const view = new DataView(ikm.buffer);
  view.setUint32(0, machine.byteLength, false);
  ikm.set(machine, 4);
  view.setUint32(4 + machine.byteLength, secret.byteLength, false);
  ikm.set(secret, 8 + machine.byteLength);

  // HKDF-SHA256 Extract + Expand (RFC 5869), salt pre-hashed as in Python
  const saltSha = new Uint8Array(await crypto.subtle.digest("SHA-256", HKDF_SALT.buffer as ArrayBuffer));
  const prk = await hmacSha256(saltSha, ikm);
  return hkdfExpand(prk, HKDF_INFO, 32);
}

/** Imports a raw byte array as an AES-GCM CryptoKey usable for wrap/unwrap. */
async function importWrappingKey(rawKey: Uint8Array): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    rawKey.buffer as ArrayBuffer,
    { name: "AES-GCM" } as AesGcmParams,
    false,
    ["encrypt", "decrypt"] as KeyUsage[]
  );
}

// ── Master key management ────────────────────────────────────────

/**
 * Unwraps the on-disk keyfile (version byte || nonce || ciphertext+tag) into
 * the master CryptoKey.  Fail-closed on unknown versions like Python — the
 * backend will (re)write a v3 keyfile and the retry loop picks it up.
 */
async function unwrapMasterKey(keyfileBytes: Uint8Array): Promise<CryptoKey> {
  if (
    keyfileBytes.byteLength < 1 + NONCE_LENGTH + KEY_LENGTH + TAG_LENGTH ||
    keyfileBytes[0] !== KEYFILE_VERSION
  ) {
    throw new Error("Unsupported keyfile version (expected scheme v3)");
  }
  const nonce = keyfileBytes.slice(1, 1 + NONCE_LENGTH);
  const ct = keyfileBytes.slice(1 + NONCE_LENGTH);  // ciphertext + tag

  const wrappingKeyRaw = await deriveWrappingKeyRaw();
  const wrappingKey = await importWrappingKey(wrappingKeyRaw);

  const masterKeyRaw = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: nonce.buffer as ArrayBuffer, additionalData: WRAP_CTX_AAD.buffer as ArrayBuffer } as AesGcmParams,
    wrappingKey,
    ct.buffer as ArrayBuffer
  );

  return crypto.subtle.importKey(
    "raw",
    masterKeyRaw,
    { name: "AES-GCM" } as AesGcmParams,
    false,
    ["encrypt", "decrypt"] as KeyUsage[]
  );
}

/**
 * Initialise the crypto layer — reads and unwraps the keyfile.
 * Must be called before encrypt/decrypt.
 */
export async function initCrypto(): Promise<void> {
  if (_ready) return;
  // A missing keyfile is a *retryable* state, not a final one: on a fresh
  // install the backend may not have written <data_dir>/keys/keyfile yet when
  // the window first connects. Throttle the retries so a permanently missing
  // key cannot turn into a disk read per message.
  const now = Date.now();
  if (now - _lastInitAttempt < INIT_RETRY_INTERVAL_MS) return;
  _lastInitAttempt = now;

  try {
    const keyfileBytes = await window.electronAPI?.readKeyfile();
    if (!keyfileBytes || keyfileBytes.byteLength === 0) {
      // The TS layer cannot create the keyfile (that requires Python's
      // `cryptography`); the server writes it. Stay un-ready so a later
      // connect() retries rather than silently running in plaintext.
      console.warn("[encre/crypto] Keyfile not available yet — transport encryption pending");
      return;
    }

    _masterKey = await unwrapMasterKey(new Uint8Array(keyfileBytes));
    _ready = true;
  } catch (err) {
    console.warn("[encre/crypto] Failed to initialise crypto (will retry):", err);
  }
}

// ── Encryption / decryption ──────────────────────────────────────

/** Generates a fresh random 12-byte GCM nonce. */
function getRandomNonce(): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(NONCE_LENGTH));
}

/**
 * Returns the master key or throws. There is deliberately no plaintext
 * fallback: the backend refuses any unencrypted frame (handshake fail-closed),
 * so silently degrading to plaintext would just break the connection in a
 * harder-to-diagnose way.
 */
function requireMasterKey(): CryptoKey {
  if (!_masterKey) {
    throw new Error("[encre/crypto] Master key unavailable — refusing to send/receive plaintext");
  }
  return _masterKey;
}

/**
 * Encrypt raw bytes → nonce(12) || ciphertext || tag(16).
 */
export async function encryptRaw(plain: Uint8Array): Promise<Uint8Array> {
  const masterKey = requireMasterKey();
  const nonce = getRandomNonce();
  const ct = new Uint8Array(await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce.buffer as ArrayBuffer } as AesGcmParams,
    masterKey,
    plain.buffer as ArrayBuffer
  ));
  const result = new Uint8Array(NONCE_LENGTH + ct.byteLength);
  result.set(nonce, 0);
  result.set(ct, NONCE_LENGTH);
  return result;
}

/**
 * Decrypt nonce(12) || ciphertext || tag(16) → raw bytes.
 */
export async function decryptRaw(packed: Uint8Array): Promise<Uint8Array> {
  const masterKey = requireMasterKey();
  if (packed.byteLength < NONCE_LENGTH + TAG_LENGTH) {
    throw new Error("[encre/crypto] Ciphertext too short to be a valid frame");
  }
  const nonce = packed.slice(0, NONCE_LENGTH);
  const ct = packed.slice(NONCE_LENGTH);
  const plain = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: nonce.buffer as ArrayBuffer } as AesGcmParams,
    masterKey,
    ct.buffer as ArrayBuffer
  );
  return new Uint8Array(plain);
}

/**
 * Convert bytes to base64 without blowing the call stack on large payloads
 * (spreading megabytes into String.fromCharCode overflows it).
 */
function toBase64(bytes: Uint8Array): string {
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK) as unknown as number[]);
  }
  return btoa(bin);
}

/**
 * base64 → bytes, chunked so large frames do not overflow the argument limit
 * of String.fromCharCode.apply. `Uint8Array.from(atob(s), cb)` is markedly
 * slower (per-character callback) and this runs on every inbound frame.
 */
function fromBase64(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  const CHUNK = 0x8000;
  for (let i = 0; i < bin.length; i += CHUNK) {
    const end = Math.min(i + CHUNK, bin.length);
    for (let j = i; j < end; j++) out[j] = bin.charCodeAt(j);
  }
  return out;
}

/**
 * Encrypt a UTF‑8 string → base64(nonce || ciphertext || tag).
 */
export async function encrypt(plaintext: string): Promise<string> {
  requireMasterKey();
  const plain = new TextEncoder().encode(plaintext);
  const packed = await encryptRaw(plain);
  return toBase64(packed);
}

/**
 * Decrypt base64(nonce || ciphertext || tag) → UTF‑8 string.
 *
 * Throws when the key is unavailable or the frame is not authentic ciphertext.
 * Callers must not substitute the input — that would reintroduce plaintext.
 */
export async function decrypt(ciphertext: string): Promise<string> {
  if (!_ready) await initCrypto();
  const plain = await decryptRaw(fromBase64(ciphertext));
  return new TextDecoder().decode(plain);
}

/** Check if crypto layer is active. */
export function isReady(): boolean {
  return _ready && _masterKey !== null;
}
