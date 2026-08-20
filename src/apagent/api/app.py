"""FastAPI app: a thin HTTP layer over the service.

Run it:  uvicorn apagent.api.app:app --reload
Then open http://127.0.0.1:8000

The path operations that only read the cache are async-free and instant.
POST /run invokes the LLM, so it is a plain `def` — FastAPI runs it in a
threadpool and the event loop stays responsive.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apagent.api.service import get_service

WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="AP Agent", version="0.1.0")


@app.get("/api/metrics")
def metrics() -> dict:
    return get_service().metrics()


@app.get("/api/schedule")
def schedule() -> dict:
    """The planned weekly payment runs for the APPROVEd invoices."""
    return get_service().schedule()


@app.get("/api/invoices")
def list_invoices() -> list[dict]:
    return get_service().list_cases()


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: str) -> dict:
    try:
        return get_service().get_case(invoice_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found") from None


@app.post("/api/invoices/{invoice_id}/run")
def run_invoice(invoice_id: str) -> dict:
    """Run the agent live on one invoice and return the fresh case bundle."""
    try:
        return get_service().run_case(invoice_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found") from None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


# Static assets (css/js). Mounted last so it never shadows the API routes.
app.mount("/", StaticFiles(directory=WEB), name="web")
