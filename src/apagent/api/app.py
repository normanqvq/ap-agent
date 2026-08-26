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

import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from apagent.api.service import get_service

log = logging.getLogger(__name__)

WEB = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the chat poller in this process, if one is configured.

    In-process is a requirement, not a convenience: Service is a singleton
    holding the DocumentStore in memory, and chat-harvested receipts are
    session state that never reaches disk. A bot running separately would
    record deliveries into a different store, and this console would keep
    showing the invoice on hold.

    No TELEGRAM_BOT_TOKEN means no thread and no behaviour change at all,
    which is also what keeps the test suite offline with no special casing.

    Nothing in here may prevent the console from starting. An integration
    that cannot reach its server is an integration that is down; a console
    that will not start is the whole product being down, and the mail side
    used to do exactly that when the SMTP host refused the connection.
    """
    from apagent.chat.runner import start_if_configured

    service = get_service()
    runner = start_if_configured(service.chat_harvester(), on_receipt=service.on_chat_receipt)

    from apagent.mail.adapters import SmtpSender
    from apagent.mail.directory import VendorDirectory
    from apagent.mail.runner import start_if_configured as start_mail

    mail_runner = None
    try:
        sender = SmtpSender()
        mail_from = os.getenv("APAGENT_MAIL_FROM", "")
        if sender.configured and mail_from:
            service.attach_mail(VendorDirectory.from_file(), sender, mail_from)
            # Off the critical path. Sending survives an unreachable relay
            # now, but a host that hangs rather than refuses still costs up
            # to 30 s per queued query, and the console must be up before
            # then -- a mail outage may cost the mail feature and nothing
            # else. Catching up on what was outstanding at boot is the only
            # job here: from now on a decision dispatches its own query.
            threading.Thread(
                target=service.dispatch_vendor_queries,
                daemon=True,
                name="apagent-mail-boot",
            ).start()
            mail_runner = start_mail(
                service.mail_harvester(),
                service._dispatcher,
                on_reply=service.on_vendor_reply,
                config=service.config,
            )
    except Exception:  # noqa: BLE001 - the console outlives its integrations
        log.exception("mail intake did not start; the console runs without it")
    try:
        yield
    finally:
        if runner is not None:
            runner.stop()
        if mail_runner is not None:
            mail_runner.stop()


app = FastAPI(title="AP Agent", version="0.1.0", lifespan=lifespan)

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


@app.post("/api/invoices/{invoice_id}/accept-chat-grn")
def accept_chat_grn(invoice_id: str, request: Request) -> dict:
    """A reviewer vouches for a delivery that was confirmed in a chat group.

    The manual counterpart to the automatic chat tier, and the reason it can
    afford to be strict. 409 when there is no chat confirmation to accept —
    code checks that, not the frontend. Re-runs the agent, so the answer is
    the pipeline's, not this endpoint's.
    """
    try:
        return get_service().accept_chat_grn(invoice_id, _actor(request))
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
