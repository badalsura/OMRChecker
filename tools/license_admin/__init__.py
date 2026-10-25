"""
Local web admin for issuing OMRChecker licenses — a GUI over tools/vendor_lib.

Vendor-only: it holds the RSA private key and runs on your own machine, bound to
127.0.0.1. It never ships with the app (the build bundles src/ only). It is the
seed of the hosted licensing server described in docs/licensing-design.md: the
same issuing + revocation logic, behind a web page instead of a CLI.

    python -m tools.license_admin           # http://127.0.0.1:8777
    python -m tools.license_admin --port 9000 --no-browser
"""
