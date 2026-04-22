import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(title="Digital Pathology API")
CASE_DATA_PATH = Path(__file__).resolve().parents[1] / "case-data.json"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"message": "FastAPI is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


def load_case_data():
    with CASE_DATA_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def build_case_summary(case_id, case):
    return {
        "caseId": case_id,
        "diagnosis": case.get("diagnosis"),
        "grade": case.get("grade"),
        "project": case.get("project"),
        "site": case.get("site"),
        "slidesLinked": case.get("slidesLinked"),
        "status": case.get("status"),
    }


@app.get("/cases")
def get_cases():
    case_data = load_case_data()
    cases = [
        build_case_summary(case_id, case)
        for case_id, case in case_data.items()
    ]

    return {
        "count": len(cases),
        "cases": cases,
    }


@app.get("/portal-data")
def get_portal_data():
    return load_case_data()


@app.get("/cases/{case_id}")
def get_case(case_id: str):
    case_data = load_case_data()
    case = case_data.get(case_id)

    if not case:
        raise HTTPException(status_code=404, detail=f"Case '{case_id}' not found")

    return {
        "caseId": case_id,
        **case,
    }
