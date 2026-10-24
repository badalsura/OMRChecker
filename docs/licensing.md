# Licensing

OMRChecker can be distributed as a licensed product. Each copy must be
activated with a **license file** that you (the vendor) sign with an RSA
private key. The application ships only the matching **public key** and
*verifies* licenses with it, so a license cannot be forged and a working
keygen cannot be written without your private key.

> **What this does and does not do.** Signed licenses make it infeasible to
> *forge* a license or write a keygen — that rests on RSA, not on hiding code.
> They do **not** make the executable impossible to reverse-engineer: any
> program that runs on someone else's computer can, in principle, be patched.
> The goal is to raise the cost enough that casual copying isn't worth it. The
> only approach that resists a local patch is to keep required logic on a
> server; this client-side scheme is the offline-friendly option.

## How it fits together

```
  Vendor (you)                         Customer
  ------------                         --------
  private_key.pem  --- sign --->  license.omrlic
      │                                  │ load in the Activate screen
      │ public_key.pem bundled in app ◄──┘
      ▼
  verified on the customer's machine (src/licensing)
```

- `src/licensing/` — the client side bundled in the app: fingerprint, signature
  verification, the gate every entry point calls. Ships the **public key**.
- `tools/license_tool.py` — the vendor side, on **your** machine only. Holds the
  **private key**; it is never shipped and never committed.

The license is **bound to a machine fingerprint** by default and may carry an
**expiry**, a **feature list**, a seat count and an edition. All of those are
covered by the signature.

## One-time setup

1. **Generate your key pair** (keep `private_key.pem` secret and backed up — if
   you lose it you cannot issue new licenses; if it leaks, anyone can):

   ```bash
   python tools/license_tool.py keygen --out-dir ./license-keys
   # prompts for a passphrase to encrypt the private key
   ```

2. **Embed the public key in the app** so builds can verify licenses:

   ```bash
   cp ./license-keys/public_key.pem src/licensing/public_key.pem
   ```

   `src/licensing/public_key.pem` is intentionally **not committed** (see
   `.gitignore`); copy it in before building. `license-keys/`, `*.omrlic` and
   private keys are git-ignored so they can never be committed by accident.

## Issuing a license to a customer

1. The customer opens the app. With no license it shows the **Activation**
   screen, which displays a **request** (their machine fingerprint + hostname).
   They send you that request (copy the text, or "Save request file").

2. You issue a license bound to their machine:

   ```bash
   python tools/license_tool.py issue \
       --key ./license-keys/private_key.pem \
       --request customer_request.json \
       --customer "Acme Exam Center" \
       --expires 2027-01-01 \
       --features core,ocr,api \
       --out acme.omrlic
   ```

   - `--expires` is optional (omit for perpetual); `YYYY-MM-DD` or full ISO.
   - `--features` is a comma list; `core` is always included. Use feature names
     to gate editions (e.g. `ocr`, `api`, `vlm`).
   - Instead of `--request`, you can pass `--machine <fingerprint>` directly, or
     `--floating` for a license not tied to any machine (use sparingly).

3. Send `acme.omrlic` back. The customer loads it on the Activation screen
   (**Choose file → Activate**). It is verified on their machine before being
   saved; an edited or copied-from-another-machine file is refused.

Check any license with:

```bash
python tools/license_tool.py inspect acme.omrlic --pubkey ./license-keys/public_key.pem
```

## Where the license lives

The installed license and a small state file sit next to the executable
(`<exe dir>/license/`), falling back to `%LOCALAPPDATA%\OMRChecker\license`
if the program folder is read-only — the same rule the data folder uses, so a
portable copy stays self-contained.

## How enforcement is wired in

One licensing layer underlies every entry point (`src/licensing/gate.py`):

- **Web GUI / API** — a single server-side dependency is added to the shared
  `secured` list, so **every** working endpoint (scan, jobs, export, compare,
  review, OCR…) returns **HTTP 402** when unlicensed. The `/license/*`
  endpoints and `/health` stay open so the app can show the Activation screen
  and accept a license. Editing the browser code cannot bypass this because the
  check is on the server.
- **Command line** (`main.py`) and **bulk** (`src/batch.py`) — call
  `gate.require_license()` before any sheet is read and exit with a clear
  message otherwise.

Features are checked with `gate.require_license("ocr")` / `gate.is_licensed(...)`.

The gate caches a verified result for a few minutes, so a license that expires
while a long-running server is up is still caught without re-reading the file on
every request. A system-clock rollback below the last-seen time is detected and
refused.

## Developing without a license

Set `OMR_LICENSE_DISABLE=1` for local source runs. The test suite sets this by
default (see `conftest.py`); `src/tests/test_licensing.py` re-enables the gate
with its own throwaway key. **Never** set `OMR_LICENSE_DISABLE` in a shipped
build.

Other overrides (mainly for testing and the vendor tool):

| Variable | Meaning |
|---|---|
| `OMR_LICENSE_PUBKEY` | Path to the public key to verify against |
| `OMR_LICENSE_FILE`   | Path to the installed license file |
| `OMR_LICENSE_DIR`    | Base folder for the license + state files |
| `OMR_LICENSE_DISABLE`| `1` turns the gate off (development only) |

## Rotating the key

`key_id` (default `v1`) is stored in each license. To roll keys, support more
than one public key keyed by `key_id` in `load_public_key` and issue new
licenses under a new id; old licenses keep verifying against the old key until
they expire.
