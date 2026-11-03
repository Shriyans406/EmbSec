from fastapi import FastAPI

from app.hardware.api import router as device_router

app = FastAPI(title="AEISP Backend", version="0.1.0")
app.include_router(device_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "phase": 1}