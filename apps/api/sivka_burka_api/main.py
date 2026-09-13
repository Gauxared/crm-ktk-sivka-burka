"""FastAPI application entry point for the initial environment scaffold."""

from fastapi import FastAPI

from .settings import load_settings


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Sivka-Burka API", version="0.1.0")

    @app.get("/health", tags=["operations"])
    def health() -> dict[str, str]:
        """Report process liveness only; this endpoint intentionally does not touch PostgreSQL."""
        return {"status": "ok", "service": "sivka-burka-api", "environment": settings.environment}

    return app


app = create_app()
