"""FastAPI app: a thin HTTP layer over the service.

Run it:  uvicorn apagent.api.app:app --reload
Then open http://127.0.0.1:8000

The path operations that only read the cache are async-free and instant.
POST /run invokes the LLM, so it is a plain `def` — FastAPI runs it in a
threadpool and the event loop stays responsive.

Sessions: DEMO sign-in, deliberately password-less and honest about it.
Every /api route (except login/me) requires a session cookie, so the
state-changing POSTs are not open to the world, and the cookie is
HttpOnly + SameSite=Lax which is what blunts cross-site POSTs. Sessions
live in memory: a restart signs everyone out, which is fine for a demo
and means nothing secret is ever written to disk.
"""

import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from apagent.api.service import get_service

WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="AP Agent", version="0.1.0")

# token -> display name. In memory only.
SESSIONS: dict[str, str] = {}
OPEN_PATHS = {"/api/login", "/api/logout", "/api/me"}


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") and path not in OPEN_PATHS:
        token = request.cookies.get("session")
        if not token or token not in SESSIONS:
            return JSONResponse({"detail": "not signed in"}, status_code=401)
    return await call_next(request)


class LoginBody(BaseModel):
    name: str


@app.post("/api/login")
def login(body: LoginBody, response: Response) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="a name is required")
    token = secrets.token_hex(16)
    SESSIONS[token] = name[:40]
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return {"name": SESSIONS[token]}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    SESSIONS.pop(request.cookies.get("session", ""), None)
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict:
    token = request.cookies.get("session")
    if not token or token not in SESSIONS:
        raise HTTPException(status_code=401, detail="not signed in")
    return {"name": SESSIONS[token]}


@app.get("/api/metrics")
def metrics() -> dict:
    return get_service().metrics()


@app.get("/api/schedule")
def schedule() -> dict:
    """The planned weekly payment runs for the APPROVEd invoices."""
    return get_service().schedule()


@app.get("/api/analytics")
def analytics() -> dict:
    """The eval scorecard and per-vendor rollup."""
    return get_service().analytics()


@app.get("/api/config")
def config() -> dict:
    """The code-enforced policy, read-only."""
    return get_service().config_info()


@app.get("/api/invoices")
def list_invoices() -> list[dict]:
    return get_service().list_cases()


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: str) -> dict:
    try:
        return get_service().get_case(invoice_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found") from None


@app.post("/api/invoices/upload")
def upload_invoice(file: UploadFile) -> dict:
    """Extract an uploaded invoice PDF live and run the agent on it."""
    content = file.file.read()
    try:
        return get_service().upload_invoice(file.filename or "invoice.pdf", content)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


@app.post("/api/invoices/{invoice_id}/run")
def run_invoice(invoice_id: str) -> dict:
    """Run the agent live on one invoice and return the fresh case bundle."""
    try:
        return get_service().run_case(invoice_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found") from None


def _actor(request: Request) -> str:
    """The signed-in reviewer's name, for the payment record and outbox."""
    return SESSIONS.get(request.cookies.get("session", ""), "reviewer")


@app.post("/api/invoices/{invoice_id}/confirm")
def confirm_payment(invoice_id: str, request: Request) -> dict:
    """Human sign-off on an APPROVEd invoice. Code re-checks the
    precondition; a non-APPROVE is refused with 409."""
    try:
        return get_service().confirm_payment(invoice_id, _actor(request))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found") from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


@app.post("/api/invoices/{invoice_id}/send-to-human")
def send_to_human(invoice_id: str, request: Request) -> dict:
    try:
        return get_service().send_to_human(invoice_id, _actor(request))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found") from None


@app.post("/api/invoices/{invoice_id}/send-message")
def send_message(invoice_id: str, request: Request) -> dict:
    """Record the decision's system-generated message in the outbox, routed
    by code to the vendor (EMAIL) or operations (HOLD). 409 when the decision
    carries no outbound message — free-text has no path."""
    try:
        return get_service().send_outbound(invoice_id, _actor(request))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id} not found") from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


@app.get("/api/outbox")
def outbox() -> list[dict]:
    """Messages the system sent this session — recorded, not delivered."""
    return get_service().outbox()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


# Static assets (css/js). Mounted last so it never shadows the API routes.
app.mount("/", StaticFiles(directory=WEB), name="web")
