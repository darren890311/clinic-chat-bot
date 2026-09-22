from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.calendar_routes import router as calendar_router
from app.api.routes import router
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="Clinic Appointment Assistant",
    version="0.1.0",
    description="Chat and voice appointment booking for a dental practice.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(calendar_router)

# The built frontend ships inside the same container and is served from the same
# origin. One deploy target, no CORS in production, no second domain to certify.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "static"

if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")
