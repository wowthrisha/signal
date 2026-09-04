"""Signal API — entry point."""
from fastapi import FastAPI

app = FastAPI(title="Signal", version="0.1.0")


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok"}
