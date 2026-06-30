"""FastAPI application entrypoint. Single-user prototype: no auth middleware yet,
but every route derives profile_id and scopes to CURRENT_USER_ID."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import FRONTEND_ORIGINS
from .database import init_db
from .routers import attributes, onboarding, profiles, search

app = FastAPI(title="JobMatch API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(profiles.router)
app.include_router(attributes.router)
app.include_router(onboarding.router)
app.include_router(search.router)
