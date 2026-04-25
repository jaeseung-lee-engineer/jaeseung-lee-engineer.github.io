import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI(title="Digital Pathology API")
CASE_DATA_URL = os.getenv(
    "CASE_DATA_URL",
    "https://jaeseung-lee.s3.us-east-2.amazonaws.com/public-test/case-data.json",
)
DEFAULT_ALLOWED_ORIGINS = [
    "https://jaeseung-lee-engineer.github.io",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def parse_allowed_origins():
    raw_origins = os.getenv("ALLOWED_ORIGINS")
    if not raw_origins:
        return DEFAULT_ALLOWED_ORIGINS

    origins = [origin.strip() for origin in raw_origins.split(",")]
    return [origin for origin in origins if origin]

app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["Accept", "Content-Type"],
)


@app.get("/")
def root():
    return {"message": "FastAPI is running"}


@app.get("/health")
def health():
    return {"status": "ok", "caseDataConfigured": bool(CASE_DATA_URL)}


def load_case_data():
    try:
        with urlopen(CASE_DATA_URL, timeout=15) as response:
            return json.load(response)
    except HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to read case data from S3: HTTP {error.code}",
        ) from error
    except URLError as error:
        raise HTTPException(
            status_code=502,
            detail="Unable to reach the case data source.",
        ) from error


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
