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
   `.gitignore`); copy it in before building. The build compiles it into the
   app (`packaging/embed_public_key.py` writes `src/licensing/embedded_key.py`),
   and a packaged app trusts only that key: a `public_key.pem` placed next to
   the exe, `OMR_LICENSE_PUBKEY` and `OMR_LICENSE_DISABLE` are ignored there. `license-keys/`, `*.omrlic` and
   private keys are git-ignored so they can never be committed by accident.

   **In CI**, store the public key as the **`LICENSE_PUBLIC_KEY`** repository
   secret (Settings → Secrets and variables → Actions). The Nuitka workflow
   (`.github/workflows/build-windows-nuitka.yml`) writes it to
   `src/licensing/public_key.pem` before building. It is the *public* key, so a
   secret is for convenience, not confidentiality — the **private** key never
   leaves your machine and is never put in CI.

## Issuing a license to a customer

1. The customer opens the app. With no license it shows the **Activation**
   screen, which displays a **request code** such as
   `OMR1-AERUK-Z4JVP-G66AJ-DIVTY-TK6N5-5J5W` (their machine fingerprint with a
   checksum, so a mistyped code is refused). They send you that code (**Copy
   request code**), or the small text file **Save request file** writes, which
   also names the computer.

2. You issue a license bound to their machine:

   ```bash
   python tools/license_tool.py issue \
       --key ./license-keys/private_key.pem \
       --request omrchecker-license-request.txt \
       --customer "Acme Exam Center" \
       --expires 2027-01-01 \
       --features core,ocr,api \
       --out acme.omrlic
   ```

   - `--expires` is optional (omit for perpetual); `YYYY-MM-DD` or full ISO.
   - `--features` is a comma list; `core` is always included. Use feature names
     to gate editions (e.g. `ocr`, `api`, `vlm`).
   - `--request` takes any text holding the code (the saved request file, a
     pasted email) or an older JSON request. Instead, you can pass
     `--machine <fingerprint>` directly, or `--floating` for a license not tied
     to any machine (use sparingly).
   - In the License Issuer, paste the code (or the whole email) into
     **Customer's request code**, or open their request file.

3. Send `acme.omrlic` back. The customer loads it on the Activation screen
   (**Choose file → Activate**). It is verified on their machine before being
   saved; an edited or copied-from-another-machine file is refused.

The user sees their license under **License** in the top bar: who it is
licensed to, edition, features, expiry with the days left, the machine it is
bound to and its id, plus the request code for a renewal. A renewed license is
loaded there too (**Load license**). From 14 days before expiry the **License**
button shows the days left.

When a license is missing, expired, for another computer, edited, not yet
valid, or the clock was set back, the Activation screen says which, with the
date where there is one, and what to do next. A license that runs out while the
app is open brings the Activation screen back.

Check any license with:

```bash
python tools/license_tool.py inspect acme.omrlic --pubkey ./license-keys/public_key.pem
```

## The License Issuer (Windows exe)

`OMRChecker-License-Issuer.exe` is the license admin above as one program,
built by the Windows workflow (`packaging/license_issuer.spec`). It holds **no
private key**: start it, enter the path of your `private_key.pem` and its
passphrase on **Setup & keys**, and issue. A `private_key.pem` next to the exe
is filled in for you, and `--key <file>` names another. Every issued license
is also saved under `license-issuer\issued\` next to the exe, beside the
revocation list. Close its console window to stop it.

## Windows builds and releases

Run **Build Windows portable exe** (Actions → workflow_dispatch) with
`installer_release` set to a tag, e.g. `v1.0.0-test1` (a tag with a dash is
published as a pre-release). It builds the app (modern and legacy zips, online
installer), the server build (portable zip, single exe, installer) and the
License Issuer, and attaches them all to that release. Run **Build Windows exe
(Nuitka)** with `release_tag` set to the same tag to add the Nuitka zips.
Both need the `LICENSE_PUBLIC_KEY` secret; without it the builds accept no
license at all.

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

Set `OMR_LICENSE_DISABLE=1` for local source runs (a packaged build ignores it). The test suite sets this by
default (see `conftest.py`); `src/tests/test_licensing.py` re-enables the gate
with its own throwaway key. **Never** set `OMR_LICENSE_DISABLE` in a shipped
build.

Other overrides (mainly for testing and the vendor tool):

| Variable | Meaning |
|---|---|
| `OMR_LICENSE_PUBKEY` | Path to the public key to verify against (source runs only) |
| `OMR_LICENSE_FILE`   | Path to the installed license file |
| `OMR_LICENSE_DIR`    | Base folder for the license + state files |
| `OMR_LICENSE_DISABLE`| `1` turns the gate off (source runs only) |
| `OMR_LICENSE_ENFORCE`| `1` turns the gate on in a source run (to test it) |

## Rotating the key

`key_id` (default `v1`) is stored in each license. To roll keys, support more
than one public key keyed by `key_id` in `load_public_key` and issue new
licenses under a new id; old licenses keep verifying against the old key until
they expire.
