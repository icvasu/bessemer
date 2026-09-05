"""Vercel entrypoint. Re-exports the same FastAPI app local uvicorn uses."""

from app.api import app
