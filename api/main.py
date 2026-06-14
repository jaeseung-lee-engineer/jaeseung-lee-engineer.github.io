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
SVS_DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("SVS_DOWNLOAD_TIMEOUT_SECONDS", "300"))
SVS_CACHE_DIR = Path(
    os.getenv(
        "SVS_CACHE_DIR",
        os.path.join(tempfile.gettempdir(), "digital-pathology-svs-cache"),
    )
)
ANALYSIS_MAX_DIMENSION = int(os.getenv("CLASSICAL_ANALYSIS_MAX_DIMENSION", "1024"))
ANALYSIS_BLUR_RADIUS = int(os.getenv("CLASSICAL_ANALYSIS_BLUR_RADIUS", "2"))
ANALYSIS_PEAK_PERCENTILE = float(os.getenv("CLASSICAL_ANALYSIS_PEAK_PERCENTILE", "92"))
ANALYSIS_MIN_DISTANCE = int(os.getenv("CLASSICAL_ANALYSIS_MIN_DISTANCE", "8"))
ANALYSIS_MAX_CELLS = int(os.getenv("CLASSICAL_ANALYSIS_MAX_CELLS", "4000"))
_case_data_cache = {
    "loaded_at": 0.0,
    "payload": None,
}
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
        "analysisMode": "classical-peaks",
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


def downsample_image(image_rgb: np.ndarray):
    height, width = image_rgb.shape[:2]
    longest_side = max(height, width)
    if longest_side <= ANALYSIS_MAX_DIMENSION:
        return image_rgb.astype(np.float32, copy=False) / 255.0, 1

    scale = int(np.ceil(longest_side / ANALYSIS_MAX_DIMENSION))
    reduced = image_rgb[::scale, ::scale].astype(np.float32, copy=False) / 255.0
    return reduced, scale


def box_blur(image: np.ndarray, radius: int):
    if radius <= 0:
        return image

    padded = np.pad(image, radius, mode="reflect")
    integral = np.pad(padded, ((1, 0), (1, 0)), mode="constant").cumsum(axis=0).cumsum(axis=1)
    size = (radius * 2) + 1
    blurred = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    ) / float(size * size)
    return blurred


def compute_nucleus_score(image_rgb: np.ndarray):
    red = image_rgb[..., 0]
    green = image_rgb[..., 1]
    blue = image_rgb[..., 2]
    intensity = (red + green + blue) / 3.0
    purple_bias = ((red + blue) * 0.5) - green
    darkness = 1.0 - intensity
    score = (purple_bias * 0.65) + (darkness * 0.85)
    return np.clip(score, 0.0, 1.0)


def find_local_maxima(score_map: np.ndarray, threshold: float):
    if score_map.shape[0] < 3 or score_map.shape[1] < 3:
        return np.zeros_like(score_map, dtype=bool)

    center = score_map[1:-1, 1:-1]
    maxima = center >= threshold
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            neighbor = score_map[1 + dy:score_map.shape[0] - 1 + dy, 1 + dx:score_map.shape[1] - 1 + dx]
            maxima &= center >= neighbor

    output = np.zeros_like(score_map, dtype=bool)
    output[1:-1, 1:-1] = maxima
    return output


def filter_peaks(score_map: np.ndarray, peak_mask: np.ndarray, scale: int, roi_origin: tuple[int, int]):
    peak_rows, peak_cols = np.nonzero(peak_mask)
    if peak_rows.size == 0:
        return []

    peak_scores = score_map[peak_rows, peak_cols]
    order = np.argsort(peak_scores)[::-1]
    occupancy = np.zeros_like(peak_mask, dtype=bool)
    min_distance = max(2, int(np.ceil(ANALYSIS_MIN_DISTANCE / scale)))
    cells = []
    offset_x, offset_y = roi_origin

    for index in order:
        row = int(peak_rows[index])
        col = int(peak_cols[index])
        top = max(0, row - min_distance)
        bottom = min(occupancy.shape[0], row + min_distance + 1)
        left = max(0, col - min_distance)
        right = min(occupancy.shape[1], col + min_distance + 1)

        if occupancy[top:bottom, left:right].any():
            continue

        occupancy[top:bottom, left:right] = True
        local_x = (col + 0.5) * scale
        local_y = (row + 0.5) * scale
        cells.append(
            {
                "id": len(cells) + 1,
                "x": round(offset_x + local_x, 3),
                "y": round(offset_y + local_y, 3),
                "localX": round(local_x, 3),
                "localY": round(local_y, 3),
                "score": round(float(peak_scores[index]), 4),
            }
        )
        if len(cells) >= ANALYSIS_MAX_CELLS:
            break

    return cells


def run_classical_count_on_region(image_rgb: np.ndarray, roi_origin: tuple[int, int]):
    reduced_image, scale = downsample_image(image_rgb)
    score_map = compute_nucleus_score(reduced_image)
    blurred_score = box_blur(score_map, ANALYSIS_BLUR_RADIUS)
    intensity = reduced_image.mean(axis=2)
    tissue_mask = intensity < 0.9

    if not np.any(tissue_mask):
        return {
            "cellCount": 0,
            "cells": [],
            "maskShape": [int(reduced_image.shape[1]), int(reduced_image.shape[0])],
            "scale": scale,
            "threshold": None,
        }

    tissue_scores = blurred_score[tissue_mask]
    threshold = float(np.percentile(tissue_scores, ANALYSIS_PEAK_PERCENTILE))
    peak_mask = find_local_maxima(blurred_score, threshold) & tissue_mask
    cells = filter_peaks(blurred_score, peak_mask, scale, roi_origin)
    return {
        "cellCount": len(cells),
        "cells": cells,
        "maskShape": [int(reduced_image.shape[1]), int(reduced_image.shape[0])],
        "scale": scale,
        "threshold": round(threshold, 4),
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
            x, y, width, height = normalize_roi_bounds(payload.roi, slide_reader.dimensions)
            region = slide_reader.read_region((x, y), 0, (width, height)).convert("RGB")
    except openslide.OpenSlideError as error:
        raise HTTPException(
            status_code=502,
            detail="Unable to open the cached SVS with OpenSlide.",
        ) from error

    analysis = run_classical_count_on_region(np.asarray(region), (x, y))
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
            "name": "classical-peaks",
            "blurRadius": ANALYSIS_BLUR_RADIUS,
            "peakPercentile": ANALYSIS_PEAK_PERCENTILE,
            "minDistance": ANALYSIS_MIN_DISTANCE,
            "scale": analysis["scale"],
            "threshold": analysis["threshold"],
        },
        "source": {
            "svsUrl": svs_url,
            "cachedFilename": cached_svs_path.name,
        },
        "timingMs": elapsed_ms,
    }
