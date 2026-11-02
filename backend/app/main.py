from fastapi import FastAPI

app = FastAPI(title="AEISP Backend", version="0.0.1")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "phase": 0}
