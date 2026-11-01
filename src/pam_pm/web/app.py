"""FastAPI web console. TODO: implement setup wizard and screens."""

from fastapi import FastAPI

app = FastAPI(title="Pam")


@app.get("/")
def health():
    """Health check."""
    return {"status": "ok"}
