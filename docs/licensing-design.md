# OMRChecker licensing — design & decisions

This records how licensing works today and the decisions behind it, so the
remaining online pieces can be finished later without re-deriving any of it.

## The model

Licenses are **RSA-signed offline files** (`.omrlic`). The vendor holds the
**private key** and signs each license; the app ships only the **public key**
(`src/licensing/public_key.pem`) and verifies signatures with it.

- Forging a license or writing a keygen requires the private key, which never
  leaves the vendor's machine and is never in anything we distribute.
- Everything in the payload (customer, expiry, machine binding, features) is
  covered by the signature, so editing any field invalidates the license.
- Signature scheme: RSA-PSS / SHA-256 over a canonical, domain-separated
  encoding of the payload (`src/licensing/model.py::signing_bytes`). The signer
  and verifier share that one definition, so they can never drift.

### Where the private key lives — the one rule that matters

The private key stays **only** on the vendor's machine, inside the admin tools
under `tools/` (`tools/vendor_lib.py`, `tools/license_tool.py`,
`tools/license_admin/`). The app bundles `src/` only (see `packaging/omr.spec`
and `packaging/build_nuitka.py`), so no path that can touch the private key is
ever shipped. If the private key were embedded in the distributed app, anyone
who unpacked the binary could extract it and mint unlimited valid licenses — the
whole scheme would be void. It must never be bundled.

## When the gate runs — desktop app vs. API/server

Decision: the license gate protects the **packaged desktop app** (the
distributed product), **not** a plain `python main.py` server/API run. An API
consumed programmatically should be gated by account/API-key permissions tied
to payment, not by a per-machine license file.

Implemented in one place, `src/licensing/gate.py::licensing_active()`:

| Situation | Gate |
|---|---|
| Packaged desktop build (`sys.frozen`) | **on** |
| Plain source/server run | **off** |
| `OMR_LICENSE_ENFORCE=1` | forced **on** (test the gate, or run a licensed server deliberately) |
| `OMR_LICENSE_DISABLE=1` | forced **off** (local dev, the test suite) |

The API choke point itself is unchanged: when the gate is active, the license
guard sits on the shared `secured` dependency list in `src/api/app.py`, so every
real endpoint returns HTTP 402 until activation while `/license/*` and `/health`
stay open. The only change is *whether* that guard enforces, decided by
`licensing_active()`.

For now the server is run by us and kept internal; stronger, account-based
server licensing comes when we distribute to organizations.

## Anti clock-rollback (the in-app clock)

Time-limited licenses must survive someone setting the system date back. The app
keeps a monotonic **last-seen time** in `.license_state` next to the license
(`src/licensing/verify.py`):

- Every successful verification advances `last_seen` (it only ever moves
  forward). The per-process gate re-verifies on its cache refresh, so a running
  app advances it periodically.
- If the clock is later found more than a slack window (2 days) behind
  `last_seen`, verification fails with `CLOCK_ROLLBACK` — winding the date back
  to dodge an expiry is caught.
- The state file is best-effort: a failure to write it never blocks a
  legitimate user, and it is a speed bump, not a vault (a determined user with
  the machine can delete it; that is accepted — the honest goal is to stop
  casual date-changing).

## Remote revocation + grace — built, DORMANT

`src/licensing/online.py` adds the piece offline files can't do: revoking a
license after issue. It is inert until a server is configured, so the current
product makes no network calls.

- Enabled by setting `OMR_LICENSE_SERVER=https://…`. Then a locally-valid
  license is also checked against the server's revocation list, at most once per
  `OMR_LICENSE_CHECK_INTERVAL_HOURS` (default 24).
- The server answers `POST /check {license_id, machine, product}` with
  `{"revoked": bool}`. A revoked license is refused (`REVOKED`).
- **Grace window** `OMR_LICENSE_GRACE_DAYS` (default 14): if the server can't be
  reached, the app keeps working since the last successful check, so flaky
  connectivity never locks out an exam centre. Past grace with no contact it
  currently falls back to the valid offline result rather than hard-failing —
  tighten to a refusal when moving to strict online-only licensing.
- The revocation list the server would serve is **the same list** the vendor
  maintains today with `tools/license_tool.py revoke` or the web admin, so
  offline and online share one source of truth.

### To go online later (checklist)

1. Host a small service exposing `POST /check` backed by the revocation list
   (the admin's `license-keys/revocations.json` is the seed format).
2. Optionally add issuing to that service (reuse `tools/vendor_lib.py`) so
   licenses are minted server-side; the private key then lives on that server
   only.
3. Set `OMR_LICENSE_SERVER` in the shipped build (or a config the app reads).
4. Decide the post-grace policy (soft fallback vs. hard refuse) in
   `online.apply_revocation`.

## Vendor tools

- **`tools/vendor_lib.py`** — the one place with private-key logic: keygen,
  signing/issuing, inspect, and the revocation list helpers. Both tools below
  use it.
- **`tools/license_tool.py`** — CLI: `keygen`, `issue`, `inspect`, `revoke`,
  `unrevoke`, `revocations`. Scriptable/offline.
- **`tools/license_admin/`** — local web admin GUI (`python -m tools.license_admin`,
  binds `127.0.0.1:8777`): load/generate keys, issue from a pasted activation
  request, inspect, and manage the revocation list. The loaded private key stays
  in process memory for the session. This is the seed of the future hosted admin
  page — the same logic, behind a web UI.

## Customer-facing activation (already in the app)

The desktop app shows an activation screen that calls `/license/request` (its
machine fingerprint), the customer sends that to the vendor, the vendor issues a
`.omrlic`, and the customer loads it via `/license/activate`. Status is
`/license/status`.

## Build / CI

- The public key is written into the build from the `LICENSE_PUBLIC_KEY` GitHub
  Actions secret (see `docs/licensing.md`); only the public key goes to CI.
- `tools/` is not bundled, so the admin and the private-key code never ship.
