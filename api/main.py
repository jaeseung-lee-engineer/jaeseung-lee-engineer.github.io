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

import cv2
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
MAX_ROI_DIMENSION = int(os.getenv("MAX_ROI_DIMENSION", "3000"))
SVS_DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("SVS_DOWNLOAD_TIMEOUT_SECONDS", "300"))
SVS_CACHE_DIR = Path(
    os.getenv(
        "SVS_CACHE_DIR",
        os.path.join(tempfile.gettempdir(), "digital-pathology-svs-cache"),
    )
)
ANALYSIS_MAX_DIMENSION = int(os.getenv("CLASSICAL_ANALYSIS_MAX_DIMENSION", "1024"))
ANALYSIS_MIN_DISTANCE = int(os.getenv("CLASSICAL_ANALYSIS_MIN_DISTANCE", "8"))
ANALYSIS_MAX_CELLS = int(os.getenv("CLASSICAL_ANALYSIS_MAX_CELLS", "4000"))
ANALYSIS_TILE_SIZE = int(os.getenv("CLASSICAL_ANALYSIS_TILE_SIZE", "512"))
ANALYSIS_TILE_OVERLAP = int(os.getenv("CLASSICAL_ANALYSIS_TILE_OVERLAP", "32"))
ANALYSIS_GAUSSIAN_KERNEL = int(os.getenv("OPENCV_GAUSSIAN_KERNEL", "5"))
ANALYSIS_DISTANCE_RATIO = float(os.getenv("OPENCV_DISTANCE_RATIO", "0.28"))
ANALYSIS_MIN_AREA = int(os.getenv("OPENCV_MIN_AREA", "24"))
ANALYSIS_MAX_AREA = int(os.getenv("OPENCV_MAX_AREA", "5000"))
ANALYSIS_MORPH_KERNEL = int(os.getenv("OPENCV_MORPH_KERNEL", "3"))
ANALYSIS_CONTOUR_EPSILON = float(os.getenv("OPENCV_CONTOUR_EPSILON", "1.2"))
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
        "analysisMode": "opencv-watershed-centroids",
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


def clamp_tile_config():
    tile_size = max(128, min(ANALYSIS_TILE_SIZE, MAX_ROI_DIMENSION))
    overlap = max(0, min(ANALYSIS_TILE_OVERLAP, tile_size // 2))
    return tile_size, overlap


def normalize_kernel_size(value: int):
    size = max(1, int(value))
    return size if size % 2 == 1 else size + 1


def downsample_image(image_rgb: np.ndarray):
    height, width = image_rgb.shape[:2]
    longest_side = max(height, width)
    if longest_side <= ANALYSIS_MAX_DIMENSION:
        return image_rgb.copy(), 1

    scale = int(np.ceil(longest_side / ANALYSIS_MAX_DIMENSION))
    reduced = cv2.resize(
        image_rgb,
        (max(1, width // scale), max(1, height // scale)),
        interpolation=cv2.INTER_AREA,
    )
    return reduced, scale


def build_nucleus_score_image(image_rgb: np.ndarray):
    image_float = image_rgb.astype(np.float32, copy=False) / 255.0
    red = image_float[..., 0]
    green = image_float[..., 1]
    blue = image_float[..., 2]
    intensity = (red + green + blue) / 3.0
    purple_bias = ((red + blue) * 0.5) - green
    darkness = 1.0 - intensity
    score = np.clip((purple_bias * 0.7) + (darkness * 0.8), 0.0, 1.0)
    return np.clip(score * 255.0, 0, 255).astype(np.uint8)


def run_opencv_count_on_region(image_rgb: np.ndarray, roi_origin: tuple[int, int]):
    reduced_image, scale = downsample_image(image_rgb)
    score_image = build_nucleus_score_image(reduced_image)
    blur_kernel = normalize_kernel_size(ANALYSIS_GAUSSIAN_KERNEL)
    morph_kernel_size = max(1, ANALYSIS_MORPH_KERNEL)

    blurred = cv2.GaussianBlur(score_image, (blur_kernel, blur_kernel), 0)
    _threshold, binary = cv2.threshold(
        blurred,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )

    morph_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (morph_kernel_size, morph_kernel_size),
    )
    opened = cv2.morphologyEx(binary, cv2.MORPH_OPEN, morph_kernel, iterations=1)
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, morph_kernel, iterations=1)

    if cv2.countNonZero(closed) == 0:
        return {
            "cellCount": 0,
            "cells": [],
            "maskShape": [int(reduced_image.shape[1]), int(reduced_image.shape[0])],
            "scale": scale,
            "threshold": None,
        }

    distance = cv2.distanceTransform(closed, cv2.DIST_L2, 5)
    max_distance = float(distance.max())
    if max_distance <= 0:
        return {
            "cellCount": 0,
            "cells": [],
            "maskShape": [int(reduced_image.shape[1]), int(reduced_image.shape[0])],
            "scale": scale,
            "threshold": None,
        }

    _, sure_fg = cv2.threshold(
        distance,
        ANALYSIS_DISTANCE_RATIO * max_distance,
        255,
        cv2.THRESH_BINARY,
    )
    sure_fg = sure_fg.astype(np.uint8)
    unknown = cv2.subtract(closed, sure_fg)

    component_count, markers = cv2.connectedComponents(sure_fg)
    markers = markers + 1
    markers[unknown == 255] = 0

    watershed_input = cv2.cvtColor(reduced_image, cv2.COLOR_RGB2BGR)
    watershed_markers = cv2.watershed(watershed_input, markers)

    offset_x, offset_y = roi_origin
    cells = []

    for label_id in range(2, component_count + 1):
        component_mask = watershed_markers == label_id
        area = int(np.count_nonzero(component_mask))
        if area < ANALYSIS_MIN_AREA or area > ANALYSIS_MAX_AREA:
            continue

        ys, xs = np.nonzero(component_mask)
        if xs.size == 0:
            continue

        contour_mask = component_mask.astype(np.uint8) * 255
        contours, _hierarchy = cv2.findContours(
            contour_mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        if not contours:
            continue

        contour = max(contours, key=cv2.contourArea)
        epsilon = max(0.5, ANALYSIS_CONTOUR_EPSILON)
        approx_contour = cv2.approxPolyDP(contour, epsilon, True)
        contour_points = [
            [
                round(offset_x + (float(point[0][0]) * scale), 3),
                round(offset_y + (float(point[0][1]) * scale), 3),
            ]
            for point in approx_contour
        ]
        if len(contour_points) < 3:
            continue

        local_x = float(xs.mean()) * scale
        local_y = float(ys.mean()) * scale
        cells.append(
            {
                "id": len(cells) + 1,
                "x": round(offset_x + local_x, 3),
                "y": round(offset_y + local_y, 3),
                "localX": round(local_x, 3),
                "localY": round(local_y, 3),
                "score": area,
                "area": area,
                "contour": contour_points,
            }
        )

        if len(cells) >= ANALYSIS_MAX_CELLS:
            break

    return {
        "cellCount": len(cells),
        "cells": cells,
        "maskShape": [int(reduced_image.shape[1]), int(reduced_image.shape[0])],
        "scale": scale,
        "threshold": None,
    }


def deduplicate_cells(cells: list[dict], min_distance: float):
    if not cells:
        return []

    ordered = sorted(cells, key=lambda cell: cell.get("score", 0.0), reverse=True)
    deduped = []
    min_distance_sq = float(min_distance * min_distance)

    for cell in ordered:
        duplicate_found = False
        for kept in deduped:
            dx = cell["x"] - kept["x"]
            dy = cell["y"] - kept["y"]
            if (dx * dx) + (dy * dy) <= min_distance_sq:
                duplicate_found = True
                break
        if duplicate_found:
            continue
        deduped.append(cell)
        if len(deduped) >= ANALYSIS_MAX_CELLS:
            break

    for index, cell in enumerate(deduped, start=1):
        cell["id"] = index

    return deduped


def iter_roi_tiles(x: int, y: int, width: int, height: int):
    tile_size, overlap = clamp_tile_config()
    step = max(1, tile_size - overlap)
    max_x = x + width
    max_y = y + height

    tile_y = y
    while tile_y < max_y:
        tile_x = x
        next_y = min(tile_y + tile_size, max_y)
        while tile_x < max_x:
            next_x = min(tile_x + tile_size, max_x)
            yield tile_x, tile_y, next_x - tile_x, next_y - tile_y
            if next_x >= max_x:
                break
            tile_x += step
        if next_y >= max_y:
            break
        tile_y += step


def run_tiled_count(slide_reader: openslide.OpenSlide, roi_bounds: tuple[int, int, int, int]):
    x, y, width, height = roi_bounds
    all_cells = []
    tile_count = 0
    scales = []
    max_mask_width = 0
    max_mask_height = 0

    for tile_x, tile_y, tile_width, tile_height in iter_roi_tiles(x, y, width, height):
        tile_count += 1
        region = slide_reader.read_region((tile_x, tile_y), 0, (tile_width, tile_height)).convert("RGB")
        analysis = run_opencv_count_on_region(np.asarray(region), (tile_x, tile_y))
        all_cells.extend(analysis["cells"])
        scales.append(analysis["scale"])
        max_mask_width = max(max_mask_width, int(analysis["maskShape"][0]))
        max_mask_height = max(max_mask_height, int(analysis["maskShape"][1]))

        if len(all_cells) >= ANALYSIS_MAX_CELLS * 3:
            break

    deduped_cells = deduplicate_cells(all_cells, ANALYSIS_MIN_DISTANCE)
    tile_size, overlap = clamp_tile_config()
    return {
        "cellCount": len(deduped_cells),
        "cells": deduped_cells,
        "maskShape": [max_mask_width, max_mask_height],
        "tileCount": tile_count,
        "tileSize": tile_size,
        "tileOverlap": overlap,
        "scale": int(round(float(np.mean(scales)))) if scales else 1,
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
            analysis = run_tiled_count(slide_reader, (x, y, width, height))
    except openslide.OpenSlideError as error:
        raise HTTPException(
            status_code=502,
            detail="Unable to open the cached SVS with OpenSlide.",
        ) from error

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
        "overlayType": "contours",
        "maskShape": analysis["maskShape"],
        "model": {
            "name": "opencv-watershed-centroids",
            "gaussianKernel": normalize_kernel_size(ANALYSIS_GAUSSIAN_KERNEL),
            "distanceRatio": ANALYSIS_DISTANCE_RATIO,
            "minDistance": ANALYSIS_MIN_DISTANCE,
            "minArea": ANALYSIS_MIN_AREA,
            "maxArea": ANALYSIS_MAX_AREA,
            "morphKernel": ANALYSIS_MORPH_KERNEL,
            "contourEpsilon": ANALYSIS_CONTOUR_EPSILON,
            "tileSize": analysis["tileSize"],
            "tileOverlap": analysis["tileOverlap"],
            "tileCount": analysis["tileCount"],
            "scale": analysis["scale"],
        },
        "source": {
            "svsUrl": svs_url,
            "cachedFilename": cached_svs_path.name,
        },
        "timingMs": elapsed_ms,
    }
