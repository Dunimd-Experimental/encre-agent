---
name: tool-hash-crypto
description: "Hashes (MD5/SHA1/SHA256/SHA512), HMAC, file checksums, AES encrypt/decrypt (CBC/ECB/CTR/GCM), and base64 encode/decode in one structured tool."
hidden: true
context: inline
---

# tool-hash-crypto

`hash_crypto` performs the checksum and symmetric-crypto jobs that otherwise turn into `openssl` invocations or Python one-liners. It hashes strings or files, computes HMACs and multi-algorithm checksums, runs AES in CBC/ECB/CTR/GCM, and converts base64 both ways. Everything returns structured JSON, and file hashing streams in 64KB chunks so large files do not get loaded into memory.

## When to Use

- Verify integrity: `action="checksum"` with `algorithm="sha256,sha1"` on a downloaded or built file.
- Fingerprint a value or file for comparison or caching: `action="hash"`.
- Sign a payload with a shared secret: `action="hmac"`.
- Encrypt a secret or fixture with a passphrase: `action="encrypt"`, keep the returned `iv` (and `tag` for GCM).
- Reverse it: `action="decrypt"` with the same `key`, `mode`, `iv` and `tag`.
- Quick conversions: `action="base64_encode"` / `action="base64_decode"`.

## When NOT to Use

- Password storage — use a slow KDF (argon2/bcrypt); plain SHA-256 is the wrong primitive.
- TLS, certificates or public-key crypto — use a dedicated library, not this tool.
- Production key management — use a vault; passphrases passed here appear in transcripts.
- Compressing or archiving files — use `archive`.
- Detecting whether two files differ — use `diff` (this tool only fingerprints).

## Key Parameters

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `action` | string | yes | `hash`, `hmac`, `checksum`, `encrypt`, `decrypt`, `base64_encode`, `base64_decode`. |
| `algorithm` | string | no | `md5`, `sha1`, `sha256` (default), `sha512`; comma-separated for `checksum`. |
| `data` | string | no | Text payload for hash/hmac/encrypt/decrypt/base64. For `decrypt`, the base64 ciphertext. |
| `file_path` | string | no | Absolute path for `hash`/`checksum` (streamed) and for `base64_encode`/`base64_decode` (read/write bytes). |
| `key` | string | no | Passphrase for `hmac`/`encrypt`/`decrypt`; the AES key is derived as `sha256(key)`. |
| `mode` | string | no | AES mode: `CBC` (default), `ECB`, `CTR`, `GCM`. |
| `encoding` | string | no | Output encoding for hash/hmac: `hex` (default) or `base64`. |
| `iv` | string | no | Base64 IV returned by `encrypt`; required by `decrypt`. |
| `tag` | string | no | Base64 auth tag from GCM `encrypt`; required by GCM `decrypt`. |

### Parameter Notes

- Supply **either** `data` **or** `file_path`; for `base64_decode`, `file_path` is the destination file and `data` the encoded input.
- The AES key is `sha256(key)`, not the raw passphrase — the same string always yields the same key, so passphrase strength is the only entropy.
- `iv` and `tag` are outputs of `encrypt` you must reuse verbatim; there is no default IV.
- `encoding` affects only hash/hmac output, not ciphertext (always base64).

## Example

```json
{
  "action": "checksum",
  "file_path": "D:/build/encre-desktop-0.5.0.zip",
  "algorithm": "sha256,sha1"
}
```

## Best Practices

- Use `checksum` with several algorithms in one call rather than repeating `hash` per algorithm.
- Prefer `mode="GCM"` for anything security-relevant: it authenticates, and `decrypt` will fail on tampering.
- Store `iv` and `tag` next to the ciphertext — losing either makes decryption impossible.
- Hash by `file_path` for large files; `data` embeds the whole payload in the request.
- Compare against a published checksum with an exact string match, case-insensitively only if the publisher is inconsistent.

## Common Pitfalls

- MD5/SHA1 are for compatibility checks only; do not present them as secure.
- ECB is deterministic and leaks structure — never for real data, despite being offered.
- Decrypting with a different `mode` or a mismatched `iv` yields garbage or an auth failure, not a clear error.
- `base64_decode` with `file_path` writes the file — it does not return the bytes.
- Hashes over text depend on exact bytes: trailing newlines and encodings change the digest.

## Anti-patterns

- Storing passwords as `sha256(password)`.
- Passing real secrets as visible `key` values in a transcript when an env-based flow exists elsewhere.
- Re-encrypting the same data to "make it safer" instead of adding authentication (GCM).
- Using this to diff two files instead of `diff`.

## Pairing with Other Tools

- `archive` — checksum a produced archive.
- `diff` — compare file contents rather than digests.
- `file_read` / `file_write` — move ciphertext or base64 payloads to disk when the tool cannot.
- `deploy` — verify artifacts after upload/download.
