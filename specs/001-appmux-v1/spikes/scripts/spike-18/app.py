"""Spike 18 fixture: a small FastAPI app that reports which copy of each package it imported."""
import importlib
import importlib.metadata as md

from fastapi import FastAPI

app = FastAPI()
NAMES = ["fastapi", "starlette", "uvicorn", "click", "pydantic", "h11", "humanize"]


@app.get("/versions")
def versions():
    out = {}
    for name in NAMES:
        try:
            module = importlib.import_module(name)
            out[name] = {"metadata_version": md.version(name), "file": module.__file__}
        except ImportError as exc:
            out[name] = {"error": type(exc).__name__}
    return out
