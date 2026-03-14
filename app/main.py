import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import img2pdf
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
CONSUME_DIR    = Path(os.getenv("CONSUME_DIR", "/consume"))
RESOLUTION     = os.getenv("RESOLUTION", "300")
SCAN_MODE      = os.getenv("SCAN_MODE", "Color")
TEMP_DIR       = Path(os.getenv("TEMP_DIR", "/tmp/simple-scan-web"))
THUMB_MAX      = (300, 400)   # max thumbnail pixel dimensions
SCAN_INTERVAL  = 300          # seconds between scanner discovery

TEMP_DIR.mkdir(parents=True, exist_ok=True)
CONSUME_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = TEMP_DIR / "session.json"
HISTORY_FILE = TEMP_DIR / "history.json"

# ── Session persistence ───────────────────────────────────────────────────────
# Single-user home use: one session at a time is enough.
scan_lock = asyncio.Lock()

session: dict = {
    "id": None,          # uuid for the current session's temp folder
    "pages": [],         # list of Path objects (scanned PNGs)
}


def save_session():
    """Write session metadata to disk so it survives restarts."""
    data = {"id": session["id"], "pages": [str(p) for p in session["pages"]]}
    SESSION_FILE.write_text(json.dumps(data))


def load_session():
    """Restore session from disk if the temp files are still around."""
    if not SESSION_FILE.exists():
        return
    try:
        data = json.loads(SESSION_FILE.read_text())
        sid = data.get("id")
        pages = [Path(p) for p in data.get("pages", [])]
        if sid and (TEMP_DIR / sid).is_dir() and all(p.exists() for p in pages):
            session["id"] = sid
            session["pages"] = pages
            log.info("Restored session %s with %d page(s)", sid, len(pages))
        else:
            SESSION_FILE.unlink(missing_ok=True)
    except (json.JSONDecodeError, KeyError):
        SESSION_FILE.unlink(missing_ok=True)


# ── History persistence ───────────────────────────────────────────────────────
# Completed sessions are kept until their PDF leaves the consume dir.
history: list = []  # list of dicts: {id, pdf, pages, timestamp, page_count}


def save_history():
    data = [
        {"id": h["id"], "pdf": str(h["pdf"]), "timestamp": h["timestamp"],
         "page_count": h["page_count"], "pages": [str(p) for p in h["pages"]]}
        for h in history
    ]
    HISTORY_FILE.write_text(json.dumps(data))


def load_history():
    if not HISTORY_FILE.exists():
        return
    try:
        for item in json.loads(HISTORY_FILE.read_text()):
            pages = [Path(p) for p in item.get("pages", [])]
            history.append({
                "id": item["id"],
                "pdf": Path(item["pdf"]),
                "timestamp": item["timestamp"],
                "page_count": item["page_count"],
                "pages": pages,
            })
        log.info("Loaded %d history entries", len(history))
    except (json.JSONDecodeError, KeyError):
        HISTORY_FILE.unlink(missing_ok=True)


def purge_stale_history():
    """Remove entries whose PDF no longer exists in the consume dir."""
    before = len(history)
    to_remove = [h for h in history if not h["pdf"].exists()]
    for h in to_remove:
        d = TEMP_DIR / h["id"]
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        history.remove(h)
    if to_remove:
        save_history()
        log.info("Purged %d stale history entries", len(to_remove))


def archive_session(pdf_path: Path):
    """Move current session into history."""
    history.append({
        "id": session["id"],
        "pdf": pdf_path,
        "pages": list(session["pages"]),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "page_count": len(session["pages"]),
    })
    save_history()


# ── Scanner discovery ─────────────────────────────────────────────────────────
scanners: dict = {
    "devices": [],       # list of {"device": str, "name": str}
    "selected": None,    # device string of the selected scanner
    "discovering": False,
}


def _parse_scanimage_list(output: str) -> list:
    """Parse `scanimage -L` output into a list of {device, name}."""
    results = []
    for line in output.splitlines():
        # Format: device `airscan:e0:EPSON ET-2980 Series' is a eSCL EPSON ET-2980 Series flatbed scanner
        m = re.match(r"device\s+[`'](.+?)'\s+is\s+(.+)", line)
        if m:
            device = m.group(1)
            description = m.group(2).strip()
            # Build a short name: backend + model from description
            backend = device.split(":")[0] if ":" in device else ""
            # Strip leading "a " or "an " from description
            name = re.sub(r'^an?\s+', '', description)
            # Strip the backend prefix (e.g. "eSCL") if it repeats
            name = re.sub(r'^[a-zA-Z]+\s+', '', name, count=1) if backend else name
            # Strip IP addresses and trailing network info
            name = re.sub(r'\s*ip=.*', '', name)
            if backend:
                name = f"{name} ({backend})"
            results.append({"device": device, "name": name})
    return results


async def discover_scanners():
    """Run scanimage -L and update the scanner list."""
    if scanners["discovering"]:
        return
    scanners["discovering"] = True
    try:
        proc = await asyncio.create_subprocess_exec(
            "scanimage", "-L",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        devices = _parse_scanimage_list(stdout.decode() + stderr.decode())
        scanners["devices"] = devices
        log.info("Discovered %d scanner(s): %s", len(devices),
                 ", ".join(d["device"] for d in devices))
        # Auto-select first if nothing selected or selected device gone
        selected_ids = [d["device"] for d in devices]
        if scanners["selected"] not in selected_ids:
            scanners["selected"] = devices[0]["device"] if devices else None
    except Exception as e:
        log.error("Scanner discovery failed: %s", e)
    finally:
        scanners["discovering"] = False


async def _scanner_poll_loop():
    """Background task: rediscover scanners periodically."""
    while True:
        await discover_scanners()
        await asyncio.sleep(SCAN_INTERVAL)


app = FastAPI(title="simple-scan-web", description="Scan to PDF — 3-button API")


@app.on_event("startup")
async def startup():
    load_session()
    load_history()
    purge_stale_history()
    asyncio.create_task(_scanner_poll_loop())


# ── Helpers ────────────────────────────────────────────────────────────────────

def session_dir() -> Optional[Path]:
    if session["id"] is None:
        return None
    return TEMP_DIR / session["id"]


def _reset_session():
    """Reset session state without deleting temp files."""
    session["id"] = None
    session["pages"] = []
    SESSION_FILE.unlink(missing_ok=True)


def clear_session():
    """Delete temp files and reset session state."""
    d = session_dir()
    if d and d.exists():
        shutil.rmtree(d, ignore_errors=True)
    _reset_session()


def new_session():
    clear_session()
    sid = str(uuid.uuid4())
    session["id"] = sid
    (TEMP_DIR / sid).mkdir(parents=True)
    save_session()
    return sid


async def run_scan() -> Path:
    """Run scanimage and return path to the saved PNG."""
    if session["id"] is None:
        raise RuntimeError("No active session")
    if not scanners["selected"]:
        raise RuntimeError("No scanner selected")

    page_num = len(session["pages"]) + 1
    out_path = session_dir() / f"page_{page_num:04d}.png"

    cmd = [
        "scanimage",
        "--device", scanners["selected"],
        "--format=png",
        f"--resolution={RESOLUTION}",
        f"--mode={SCAN_MODE}",
        f"--output-file={out_path}",
    ]
    log.info("Scanning page %d: %s", page_num, " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        log.error("scanimage failed: %s", stderr.decode())
        raise RuntimeError(f"scanimage error: {stderr.decode().strip()}")

    if not out_path.exists():
        raise RuntimeError("scanimage succeeded but output file not found")

    log.info("Page %d saved to %s", page_num, out_path)
    return out_path


def merge_to_pdf() -> Path:
    """Merge all session pages into a single PDF in the consume dir."""
    pages = session["pages"]
    if not pages:
        raise RuntimeError("No pages to merge")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_pdf = CONSUME_DIR / f"scan_{timestamp}.pdf"

    log.info("Merging %d page(s) → %s", len(pages), out_pdf)
    with open(out_pdf, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in pages]))

    log.info("PDF written: %s", out_pdf)
    return out_pdf


# ── API endpoints ──────────────────────────────────────────────────────────────

@app.post("/scan/start", summary="Start new scan session (page 1)")
async def scan_start():
    """
    Clears any previous session, starts a new one, and scans the first page.
    """
    if scan_lock.locked():
        raise HTTPException(409, "A scan is already in progress")

    async with scan_lock:
        try:
            new_session()
            page = await run_scan()
            session["pages"].append(page)
            save_session()
            return {"status": "ok", "pages": len(session["pages"]), "message": "Page 1 scanned. Use /scan/append to add more or /scan/finish to export."}
        except Exception as e:
            clear_session()
            raise HTTPException(500, str(e))


@app.post("/scan/append", summary="Scan and append next page")
async def scan_append():
    """
    Scans another page and appends it to the current session.
    """
    if session["id"] is None:
        raise HTTPException(400, "No active session — call /scan/start first")
    if scan_lock.locked():
        raise HTTPException(409, "A scan is already in progress")

    async with scan_lock:
        try:
            page = await run_scan()
            session["pages"].append(page)
            save_session()
            n = len(session["pages"])
            return {"status": "ok", "pages": n, "message": f"Page {n} scanned. Append more or call /scan/finish."}
        except Exception as e:
            raise HTTPException(500, str(e))


@app.post("/scan/finish", summary="Merge pages into PDF and export")
async def scan_finish():
    """
    Merges all scanned pages into a single PDF and drops it in the consume directory.
    Clears the session afterwards.
    """
    if session["id"] is None:
        raise HTTPException(400, "No active session — call /scan/start first")
    if scan_lock.locked():
        raise HTTPException(409, "A scan is already in progress")
    if not session["pages"]:
        raise HTTPException(400, "No pages scanned yet")

    try:
        loop = asyncio.get_event_loop()
        pdf_path = await loop.run_in_executor(None, merge_to_pdf)
        page_count = len(session["pages"])
        archive_session(pdf_path)
        _reset_session()
        return {
            "status": "ok",
            "pages": page_count,
            "output": str(pdf_path),
            "message": f"✓ {page_count}-page PDF exported.",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/scan/cancel", summary="Cancel current session without saving")
async def scan_cancel():
    """Discard current session and all scanned pages."""
    clear_session()
    return {"status": "ok", "message": "Scan discarded."}


@app.get("/scan/status", summary="Current session status")
async def scan_status():
    return {
        "active": session["id"] is not None,
        "scanning": scan_lock.locked(),
        "pages": len(session["pages"]),
        "session_id": session["id"],
    }


# ── Scanner endpoints ─────────────────────────────────────────────────────────

@app.get("/scanners", summary="List discovered scanners")
async def list_scanners():
    return {
        "devices": scanners["devices"],
        "selected": scanners["selected"],
        "discovering": scanners["discovering"],
    }


@app.post("/scanners/refresh", summary="Re-scan for scanners")
async def refresh_scanners():
    await discover_scanners()
    return {
        "devices": scanners["devices"],
        "selected": scanners["selected"],
    }


@app.post("/scanners/select", summary="Select a scanner")
async def select_scanner(device: str):
    known = [d["device"] for d in scanners["devices"]]
    if device not in known:
        raise HTTPException(400, f"Unknown device. Available: {known}")
    scanners["selected"] = device
    return {"selected": device}


PREVIEW_MAX = (1200, 1600)  # larger preview for popup


def _cache_suffix(max_size: tuple) -> str:
    return f".{max_size[0]}x{max_size[1]}"


def _generate_jpeg(page_path: Path, max_size: tuple, quality: int) -> bytes:
    """CPU-bound: open image, resize, encode to JPEG. Caches on disk."""
    cache_path = page_path.with_suffix(_cache_suffix(max_size) + ".jpg")
    if cache_path.exists():
        return cache_path.read_bytes()
    img = Image.open(page_path)
    img.thumbnail(max_size)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    data = buf.getvalue()
    cache_path.write_bytes(data)
    return data


async def _serve_page(pages: list, page_num: int, max_size: tuple, quality: int) -> Response:
    if page_num < 1 or page_num > len(pages):
        raise HTTPException(404, "Page not found")
    page_path = pages[page_num - 1]
    if not page_path.exists():
        raise HTTPException(404, "Page file not found")
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _generate_jpeg, page_path, max_size, quality)
    return Response(content=data, media_type="image/jpeg")


# ── Active session page endpoints ─────────────────────────────────────────────

@app.get("/scan/page/{page_num}", summary="Page thumbnail")
async def get_page_thumb(page_num: int):
    if session["id"] is None:
        raise HTTPException(404, "No active session")
    return await _serve_page(session["pages"], page_num, THUMB_MAX, quality=60)


@app.get("/scan/page/{page_num}/full", summary="Full page preview")
async def get_page_full(page_num: int):
    if session["id"] is None:
        raise HTTPException(404, "No active session")
    return await _serve_page(session["pages"], page_num, PREVIEW_MAX, quality=80)


# ── History endpoints ─────────────────────────────────────────────────────────

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _find_history(session_id: str) -> dict:
    if not _UUID_RE.match(session_id):
        raise HTTPException(400, "Invalid session ID")
    for h in history:
        if h["id"] == session_id:
            return h
    raise HTTPException(404, "Session not found")


@app.get("/scan/history", summary="List completed sessions")
async def get_history():
    purge_stale_history()
    return [
        {"id": h["id"], "timestamp": h["timestamp"],
         "page_count": h["page_count"], "pdf": h["pdf"].name}
        for h in reversed(history)
    ]


@app.get("/scan/history/{session_id}/page/{page_num}", summary="History thumbnail")
async def get_history_thumb(session_id: str, page_num: int):
    return await _serve_page(_find_history(session_id)["pages"], page_num, THUMB_MAX, quality=60)


@app.get("/scan/history/{session_id}/page/{page_num}/full", summary="History full preview")
async def get_history_full(session_id: str, page_num: int):
    return await _serve_page(_find_history(session_id)["pages"], page_num, PREVIEW_MAX, quality=80)


@app.get("/scan/history/{session_id}/pdf", summary="Download the PDF")
async def download_history_pdf(session_id: str):
    h = _find_history(session_id)
    if not h["pdf"].exists():
        raise HTTPException(404, "PDF no longer available")
    return FileResponse(
        path=h["pdf"],
        media_type="application/pdf",
        filename=h["pdf"].name,
    )


@app.delete("/scan/history/{session_id}", summary="Delete a history entry")
async def delete_history_entry(session_id: str):
    h = _find_history(session_id)
    d = TEMP_DIR / h["id"]
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    history.remove(h)
    save_history()
    return {"status": "ok", "message": "History entry removed."}


# ── Web UI ─────────────────────────────────────────────────────────────────────

_HTML_PATH = Path(__file__).parent / "index.html"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui():
    return FileResponse(_HTML_PATH, media_type="text/html")
