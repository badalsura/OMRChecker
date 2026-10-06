"""
REST API and web GUI for the OMR engine.

    from src.api.app import create_app
    app = create_app("./omr_data")

or from the command line:

    python -m src.api --host 0.0.0.0 --port 8000 --data-dir ./omr_data --workers 8
"""
