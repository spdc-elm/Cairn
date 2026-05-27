from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.routers import agent_context, branches, environments, executions, export, hints, intents, projects, settings, workers

STATIC_DIR = Path(__file__).parent / "static"
LOG = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(settings.router)
app.include_router(agent_context.router)
app.include_router(environments.router)
app.include_router(workers.router)
app.include_router(projects.router)
app.include_router(executions.router)
app.include_router(branches.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = [_validation_error_shape(error) for error in exc.errors()]
    event_count = await _request_event_count(request)
    LOG.warning(
        "request_validation_failed path=%s request_event_count=%s content_length=%s errors=%s",
        request.url.path,
        event_count,
        request.headers.get("content-length"),
        errors,
    )
    return JSONResponse(status_code=422, content={"detail": errors})


def _validation_error_shape(error: dict) -> dict:
    return {
        "loc": error.get("loc", ()),
        "msg": error.get("msg", "Invalid request"),
        "type": error.get("type", "value_error"),
    }


async def _request_event_count(request: Request) -> int | None:
    try:
        body = await request.body()
        payload = json.loads(body.decode("utf-8")) if body else None
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    events = payload.get("events")
    return len(events) if isinstance(events, list) else None


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
