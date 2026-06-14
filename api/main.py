import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import numpy as np
import openslide
import requests
from cellpose import models
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


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
CACHE_TTL_SECONDS = int(os.getenv("CASE_DATA_CACHE_TTL_SECONDS", "1800"))
MAX_ROI_DIMENSION = int(os.getenv("MAX_ROI_DIMENSION", "2048"))
CELLPOSE_MODEL_NAME = os.getenv("CELLPOSE_PRETRAINED_MODEL", "cpsam_v2")
CELLPOSE_DIAMETER = float(os.getenv("CELLPOSE_DIAMETER", "30"))
CELLPOSE_BATCH_SIZE = int(os.getenv("CELLPOSE_BATCH_SIZE", "8"))
CELLPOSE_MIN_SIZE = int(os.getenv("CELLPOSE_MIN_SIZE", "15"))
SVS_DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("SVS_DOWNLOAD_TIMEOUT_SECONDS", "300"))
SVS_CACHE_DIR = Path(
    os.getenv(
        "SVS_CACHE_DIR",
        os.path.join(tempfile.gettempdir(), "digital-pathology-svs-cache"),
    )
)
_case_data_cache = {
    "loaded_at": 0.0,
    "payload": None,
}
_cellpose_model = None
_cellpose_model_lock = Lock()
_svs_cache_lock = Lock()


class RoiRectRequest(BaseModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0, le=MAX_ROI_DIMENSION)
    height: float = Field(gt=0, le=MAX_ROI_DIMENSION)


class CellCountRequest(BaseModel):
    roi: RoiRectRequest


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
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Content-Type"],
)


@app.get("/")
def root():
    return {"message": "FastAPI is running"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "caseDataConfigured": bool(CASE_DATA_URL),
        "maxRoiDimension": MAX_ROI_DIMENSION,
    }


def load_case_data():
    now = time.time()
    cached_payload = _case_data_cache["payload"]
    loaded_at = _case_data_cache["loaded_at"]

    if cached_payload is not None and (now - loaded_at) < CACHE_TTL_SECONDS:
        return cached_payload

    try:
        with urlopen(CASE_DATA_URL, timeout=15) as response:
            payload = json.load(response)
            _case_data_cache["payload"] = payload
            _case_data_cache["loaded_at"] = now
            return payload
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


def find_slide_by_id(case_data, slide_id):
    for case_id, case in case_data.items():
        for slide in case.get("slides", []):
            if slide.get("slideId") == slide_id:
                return case_id, case, slide
    return None, None, None


def get_cellpose_model():
    global _cellpose_model

    if _cellpose_model is not None:
        return _cellpose_model

    with _cellpose_model_lock:
        if _cellpose_model is None:
            _cellpose_model = models.CellposeModel(
                gpu=False,
                pretrained_model=CELLPOSE_MODEL_NAME,
            )
    return _cellpose_model


def build_cached_svs_path(svs_url: str) -> Path:
    parsed = urlparse(svs_url)
    suffix = Path(parsed.path).suffix or ".svs"
    hashed = hashlib.sha256(svs_url.encode("utf-8")).hexdigest()
    return SVS_CACHE_DIR / f"{hashed}{suffix}"


def ensure_svs_cached(svs_url: str) -> Path:
    if not svs_url:
        raise HTTPException(status_code=400, detail="SVS source URL is not configured.")

    cached_path = build_cached_svs_path(svs_url)
    if cached_path.exists():
        return cached_path

    with _svs_cache_lock:
        if cached_path.exists():
            return cached_path

        SVS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        partial_path = cached_path.with_suffix(f"{cached_path.suffix}.part")

        try:
            with requests.get(
                svs_url,
                stream=True,
                timeout=(20, SVS_DOWNLOAD_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
                with partial_path.open("wb") as target:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            target.write(chunk)
            partial_path.replace(cached_path)
        except requests.RequestException as error:
            partial_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=502,
                detail="Unable to download the source SVS from storage.",
            ) from error

    return cached_path


def normalize_roi_bounds(roi: RoiRectRequest, dimensions: tuple[int, int]):
    slide_width, slide_height = dimensions
    x = int(round(roi.x))
    y = int(round(roi.y))
    width = int(round(roi.width))
    height = int(round(roi.height))

    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="ROI width and height must be positive.")

    if width > MAX_ROI_DIMENSION or height > MAX_ROI_DIMENSION:
        raise HTTPException(
            status_code=400,
            detail=f"ROI dimensions must be <= {MAX_ROI_DIMENSION}px.",
        )

    if x < 0 or y < 0 or x >= slide_width or y >= slide_height:
        raise HTTPException(status_code=400, detail="ROI origin is outside the slide bounds.")

    if x + width > slide_width or y + height > slide_height:
        raise HTTPException(status_code=400, detail="ROI extends beyond the slide bounds.")

    return x, y, width, height


def compute_cell_centroids(masks: np.ndarray, roi_origin: tuple[int, int]):
    if masks.size == 0:
        return []

    labels = masks.astype(np.int32, copy=False).ravel()
    positive = labels > 0
    if not np.any(positive):
        return []

    height, width = masks.shape
    ys, xs = np.indices((height, width))
    xs = xs.ravel()[positive].astype(np.float64, copy=False)
    ys = ys.ravel()[positive].astype(np.float64, copy=False)
    labels = labels[positive]

    counts = np.bincount(labels)
    sum_x = np.bincount(labels, weights=xs)
    sum_y = np.bincount(labels, weights=ys)
    offset_x, offset_y = roi_origin
    cells = []

    for label_id in range(1, len(counts)):
        if counts[label_id] == 0:
            continue

        local_x = sum_x[label_id] / counts[label_id]
        local_y = sum_y[label_id] / counts[label_id]
        cells.append(
            {
                "id": label_id,
                "x": round(offset_x + local_x, 3),
                "y": round(offset_y + local_y, 3),
                "localX": round(local_x, 3),
                "localY": round(local_y, 3),
                "pixelCount": int(counts[label_id]),
            }
        )

    return cells


def run_cellpose_on_region(image_rgb: np.ndarray, roi_origin: tuple[int, int]):
    model = get_cellpose_model()
    masks, flows, styles = model.eval(
        image_rgb,
        batch_size=CELLPOSE_BATCH_SIZE,
        diameter=CELLPOSE_DIAMETER,
        channels=None,
        flow_threshold=0.4,
        cellprob_threshold=0.0,
        min_size=CELLPOSE_MIN_SIZE,
        tile_overlap=0.1,
    )
    cells = compute_cell_centroids(np.asarray(masks), roi_origin)
    return {
        "cellCount": len(cells),
        "cells": cells,
        "maskShape": [int(masks.shape[1]), int(masks.shape[0])],
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


@app.post("/slides/{slide_id}/cell-count")
def count_cells_in_roi(slide_id: str, payload: CellCountRequest):
    case_data = load_case_data()
    case_id, case, slide = find_slide_by_id(case_data, slide_id)

    if not slide:
        raise HTTPException(status_code=404, detail=f"Slide '{slide_id}' not found")

    svs_url = slide.get("svsUrl")
    if not svs_url:
        raise HTTPException(
            status_code=400,
            detail=f"Slide '{slide_id}' does not include an SVS source URL.",
        )

    started_at = time.perf_counter()
    cached_svs_path = ensure_svs_cached(svs_url)

    try:
        with openslide.OpenSlide(str(cached_svs_path)) as slide_reader:
            x, y, width, height = normalize_roi_bounds(
                payload.roi,
                slide_reader.dimensions,
            )
            region = slide_reader.read_region((x, y), 0, (width, height)).convert("RGB")
    except openslide.OpenSlideError as error:
        raise HTTPException(
            status_code=502,
            detail="Unable to open the cached SVS with OpenSlide.",
        ) from error

    analysis = run_cellpose_on_region(np.asarray(region), (x, y))
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

    return {
        "caseId": case_id,
        "caseDiagnosis": case.get("diagnosis"),
        "slideId": slide_id,
        "slideLabel": slide.get("label"),
        "roi": {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        },
        "cellCount": analysis["cellCount"],
        "cells": analysis["cells"],
        "overlayType": "points",
        "maskShape": analysis["maskShape"],
        "model": {
            "name": CELLPOSE_MODEL_NAME,
            "diameter": CELLPOSE_DIAMETER,
            "minSize": CELLPOSE_MIN_SIZE,
        },
        "source": {
            "svsUrl": svs_url,
            "cachedFilename": cached_svs_path.name,
        },
        "timingMs": elapsed_ms,
    }
