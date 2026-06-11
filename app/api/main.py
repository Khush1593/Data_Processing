"""Stage 0 review UI — FastAPI app entrypoint.

Run with:
    .venv/bin/uvicorn app.api.main:app --reload --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.projects import router as projects_router
from app.db import init_db

app = FastAPI(title="Clarum Insights — Stage 0 Review")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(projects_router, prefix="/api")
