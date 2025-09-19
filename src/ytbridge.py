from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routers import discovery, playback, library

def create_app() -> FastAPI:
    app = FastAPI(title="ytbridge", version="0.7.1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(discovery.router)
    app.include_router(playback.router)
    app.include_router(library.router)
    return app

app = create_app()
