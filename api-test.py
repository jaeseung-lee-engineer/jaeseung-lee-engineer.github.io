"""
Prototype FastAPI backend for the Digital Pathology Web Portal.
This API demonstrates how case, slide, and annotation data could be served.
The implementation is partial and intended for conceptual demonstration only (not functional or production-ready).
"""

from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"message": "API is running"}

@app.get("/cases")
def get_cases():
    return [
        {"id": 1, "diagnosis": "Mixed Glioma (G3)"},
        {"id": 2, "diagnosis": "Oligodendroglioma"}
    ]
