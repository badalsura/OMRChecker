"""HTTP endpoints for OCR engines and image zone crops (src/readers)."""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from src.api.storage import read_json


def register(app, ctx, secured):
    @app.get("/ocr/capabilities", tags=["meta"], dependencies=secured)
    def ocr_capabilities():
        """OCR engines available on this server, installed languages, defaults."""
        from src.readers.text_reader import ocr_capabilities as capabilities

        return capabilities()

    @app.get(
        "/results/{scan_id}/zone-image/{zone}", tags=["results"], dependencies=secured
    )
    def zone_image(scan_id: str, zone: str):
        """The saved crop of an image zone (student photo, signature)."""
        if not scan_id.isalnum():
            raise HTTPException(404, "Scan not found")
        scan_dir = ctx.data.scan_dir(scan_id)
        result = read_json(scan_dir / "result.json")
        if result is None:
            raise HTTPException(404, "Scan not found")
        entry = (result.get("zones") or {}).get(zone) or {}
        filename = (entry.get("details") or {}).get("file")
        if entry.get("type") != "image" or not filename:
            raise HTTPException(404, f"No image for zone '{zone}'")
        path = scan_dir / "zones" / Path(filename).name
        if not path.is_file():
            raise HTTPException(404, "Zone image not saved")
        return FileResponse(str(path))
