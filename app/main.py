from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(
    title="Store Intelligence API",
    description="End-to-end CCTV-based retail conversion metrics platform.",
    version="0.1.0",
)


@app.get("/", tags=["Root"])
async def root():
    """Welcome endpoint."""
    return {"message": "Store Intelligence API is live."}


@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint.
    Returns service status and version.
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "version": "0.1.0",
            "service": "store-intelligence-api",
        },
    )
