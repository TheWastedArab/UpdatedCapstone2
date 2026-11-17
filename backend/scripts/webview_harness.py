"""Development server: the API plus the extension's webview in a normal browser.

    python -m scripts.webview_harness        then open http://127.0.0.1:8000/harness

Lets the interface be inspected (and tested for accessibility with browser
tools) without launching VS Code. Not for deployment.
"""
from pathlib import Path

import uvicorn
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.main import app

EXTENSION = Path(__file__).resolve().parents[2] / "extension"

app.mount("/harness-media", StaticFiles(directory=EXTENSION / "media"), name="harness-media")


@app.get("/harness", include_in_schema=False)
def harness():
    return FileResponse(EXTENSION / "dev" / "harness.html")


@app.get("/harness/shim.js", include_in_schema=False)
def shim():
    return FileResponse(EXTENSION / "dev" / "shim.js", media_type="text/javascript")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
