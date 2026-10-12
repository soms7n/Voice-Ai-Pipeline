"""FastAPI application entry point for the device-recovery orchestrator."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from .api import router
from .config import get_settings
from .database import SCHEMA_VERSION, initialise_database
from .services import DomainError


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialise_database()
    yield


app = FastAPI(
    title="Airtel Device-Recovery Voice AI",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.websocket("/media")
async def exotel_media(websocket: WebSocket, token: str) -> None:
    """Authenticated Exotel AgentStream endpoint for the Pipecat voice agent."""

    from .voice_agent import serve_exotel_media

    try:
        await serve_exotel_media(websocket, token)
    except DomainError:
        await websocket.close(code=1008)


@app.get("/health", tags=["operations"])
def health() -> dict[str, str | int]:
    """Return a small unauthenticated liveness and schema-readiness response."""

    settings = get_settings()
    return {
        "status": "ook",
        "environment": settings.environment,
        "schema_version": SCHEMA_VERSION,
    }
