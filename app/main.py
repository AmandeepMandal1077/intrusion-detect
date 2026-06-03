from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.anomalies import router as anomalies_router
from app.funnel import router as funnel_router
from app.ingestion import router as ingestion_router
from app.metrics import router as metrics_router

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run DB table creation on startup. Gracefully skips if DB is unavailable (e.g. tests)."""
    try:
        from app.database import init_db
        init_db()
        logger.info("Database tables initialised.")
    except Exception as exc:
        logger.warning("DB init skipped (likely test environment): %s", exc)
    yield


app = FastAPI(
    title="Store Intelligence API",
    description="End-to-end CCTV-based retail conversion metrics platform.",
    version="0.3.0",
    lifespan=lifespan,
)

# Routers
app.include_router(ingestion_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(anomalies_router)

# Core endpoints

@app.get("/", tags=["Root"])
async def root():
    """Welcome endpoint."""
    return {"message": "Store Intelligence API is live."}


@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint. Returns service status and version."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "version": "0.3.0",
            "service": "store-intelligence-api",
        },
    )
