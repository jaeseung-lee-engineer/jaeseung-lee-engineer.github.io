import hashlib
import os
import time
from pathlib import Path

import modal


APP_NAME = os.getenv("MODAL_APP_NAME", "digital-pathology-stardist")
MODEL_NAME = os.getenv("STARDIST_MODEL_NAME", "2D_versatile_he")
GPU_TYPE = os.getenv("MODAL_GPU_TYPE", "L4")
MODEL_VOLUME_NAME = os.getenv("MODAL_MODEL_VOLUME", "stardist-models")
SVS_VOLUME_NAME = os.getenv("MODAL_SVS_VOLUME", "stardist-svs-cache")
MODEL_DIR = "/models"
SVS_CACHE_DIR = "/svs-cache"
MAX_ROI_DIMENSION = int(os.getenv("MAX_ROI_DIMENSION", "3000"))
SVS_DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("SVS_DOWNLOAD_TIMEOUT_SECONDS", "300"))
DEFAULT_PERCENTILE_LOW = float(os.getenv("STARDIST_PERCENTILE_LOW", "0.2"))
DEFAULT_PERCENTILE_HIGH = float(os.getenv("STARDIST_PERCENTILE_HIGH", "99.8"))
DEFAULT_MAX_DIMENSION = int(os.getenv("STARDIST_MAX_DIMENSION", "4096"))

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0")
    .pip_install(
        "fastapi[standard]",
        "numpy",
        "opencv-python-headless",
        "openslide-bin",
        "openslide-python",
        "requests",
        "tensorflow",
        "stardist",
        "csbdeep",
        "pillow",
        "scikit-image",
    )
)

model_volume = modal.Volume.from_name(MODEL_VOLUME_NAME, create_if_missing=True)
svs_volume = modal.Volume.from_name(SVS_VOLUME_NAME, create_if_missing=True)


@app.function(
    image=image,
    gpu=GPU_TYPE,
    timeout=900,
    scaledown_window=300,
    volumes={
        MODEL_DIR: model_volume,
        SVS_CACHE_DIR: svs_volume,
    },
)
@modal.asgi_app()
def fastapi_app():
    import cv2
    import numpy as np
    import openslide
    import requests
    from csbdeep.utils import normalize
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
    from stardist.models import StarDist2D

    web_app = FastAPI(title="StarDist Modal API")
    _model = None

    class RoiRectRequest(BaseModel):
        x: float = Field(ge=0)
        y: float = Field(ge=0)
        width: float = Field(gt=0, le=MAX_ROI_DIMENSION)
        height: float = Field(gt=0, le=MAX_ROI_DIMENSION)

    class StarDistRequest(BaseModel):
        svsUrl: str
        roi: RoiRectRequest
        percentileLow: float = Field(default=DEFAULT_PERCENTILE_LOW, ge=0, le=100)
        percentileHigh: float = Field(default=DEFAULT_PERCENTILE_HIGH, ge=0, le=100)
        maxDimension: int = Field(default=DEFAULT_MAX_DIMENSION, gt=0, le=8192)

    def get_model():
        nonlocal _model
        if _model is None:
            print(f"Loading StarDist model: {MODEL_NAME}")
            _model = StarDist2D.from_pretrained(MODEL_NAME)
            model_volume.commit()
        return _model

    def build_cached_svs_path(svs_url: str) -> Path:
        suffix = Path(svs_url).suffix or ".svs"
        hashed = hashlib.sha256(svs_url.encode("utf-8")).hexdigest()
        return Path(SVS_CACHE_DIR) / f"{hashed}{suffix}"

    def ensure_svs_cached(svs_url: str) -> Path:
        if not svs_url:
            raise HTTPException(status_code=400, detail="SVS URL is required.")

        cached_path = build_cached_svs_path(svs_url)
        if cached_path.exists() and cached_path.stat().st_size > 0:
            return cached_path

        response = requests.get(svs_url, stream=True, timeout=SVS_DOWNLOAD_TIMEOUT_SECONDS)
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise HTTPException(
                status_code=502,
                detail=f"Unable to download SVS source: HTTP {response.status_code}.",
            ) from error

        temp_path = cached_path.with_suffix(f"{cached_path.suffix}.partial")
        with open(temp_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

        temp_path.replace(cached_path)
        svs_volume.commit()
        return cached_path

    def normalize_roi_bounds(roi: RoiRectRequest, dimensions: tuple[int, int]):
        slide_width, slide_height = dimensions
        x = max(0, int(round(roi.x)))
        y = max(0, int(round(roi.y)))
        width = int(round(roi.width))
        height = int(round(roi.height))

        if width <= 0 or height <= 0:
            raise HTTPException(status_code=400, detail="ROI width and height must be positive.")
        if width > MAX_ROI_DIMENSION or height > MAX_ROI_DIMENSION:
            raise HTTPException(
                status_code=400,
                detail=f"ROI dimensions must be <= {MAX_ROI_DIMENSION}px.",
            )

        x = min(x, max(0, slide_width - 1))
        y = min(y, max(0, slide_height - 1))
        width = min(width, slide_width - x)
        height = min(height, slide_height - y)
        return x, y, width, height

    def downsample_for_preprocess(image_rgb: np.ndarray, max_dimension: int):
        height, width = image_rgb.shape[:2]
        longest_side = max(width, height)
        if longest_side <= max_dimension:
            return image_rgb, 1

        scale = int(np.ceil(longest_side / max_dimension))
        reduced = cv2.resize(
            image_rgb,
            (max(1, width // scale), max(1, height // scale)),
            interpolation=cv2.INTER_AREA,
        )
        return reduced, scale

    def contour_from_mask(mask: np.ndarray, offset_x: int, offset_y: int, scale: int):
        contour_mask = mask.astype(np.uint8) * 255
        contours, _hierarchy = cv2.findContours(
            contour_mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )
        if not contours:
            return None

        contour = max(contours, key=cv2.contourArea)
        approx = cv2.approxPolyDP(contour, epsilon=1.0, closed=True)
        points = [
            [
                round(offset_x + float(point[0][0]) * scale, 3),
                round(offset_y + float(point[0][1]) * scale, 3),
            ]
            for point in approx
        ]
        return points if len(points) >= 3 else None

    def build_cells_from_labels(labels: np.ndarray, offset_x: int, offset_y: int, scale: int):
        cells = []
        label_ids = np.unique(labels)
        label_ids = label_ids[label_ids > 0]

        for cell_id, label_id in enumerate(label_ids, start=1):
            component_mask = labels == label_id
            ys, xs = np.nonzero(component_mask)
            if xs.size == 0:
                continue

            contour = contour_from_mask(component_mask, offset_x, offset_y, scale)
            if not contour:
                continue

            local_x = float(xs.mean()) * scale
            local_y = float(ys.mean()) * scale
            area = int(np.count_nonzero(component_mask))
            cells.append(
                {
                    "id": cell_id,
                    "x": round(offset_x + local_x, 3),
                    "y": round(offset_y + local_y, 3),
                    "localX": round(local_x, 3),
                    "localY": round(local_y, 3),
                    "area": area,
                    "score": area,
                    "contour": contour,
                }
            )

        return cells

    @web_app.get("/health")
    async def health():
        return {
            "status": "ok",
            "modelName": MODEL_NAME,
            "maxRoiDimension": MAX_ROI_DIMENSION,
            "gpuType": GPU_TYPE,
        }

    @web_app.post("/stardist")
    async def stardist_detect(payload: StarDistRequest):
        started_at = time.perf_counter()
        try:
            model = get_model()
        except Exception as error:  # noqa: BLE001
            print(f"StarDist model load failed: {error!r}")
            raise HTTPException(
                status_code=500,
                detail=f"StarDist model load failed: {error}",
            ) from error

        cached_svs_path = ensure_svs_cached(payload.svsUrl)

        try:
            with openslide.OpenSlide(str(cached_svs_path)) as slide_reader:
                x, y, width, height = normalize_roi_bounds(payload.roi, slide_reader.dimensions)
                region = slide_reader.read_region((x, y), 0, (width, height)).convert("RGB")
        except openslide.OpenSlideError as error:
            raise HTTPException(
                status_code=502,
                detail="Unable to open the cached SVS with OpenSlide.",
            ) from error

        image_rgb = np.asarray(region)
        processed_image, scale = downsample_for_preprocess(image_rgb, payload.maxDimension)
        normalized = normalize(processed_image, payload.percentileLow, payload.percentileHigh)

        try:
            labels, details = model.predict_instances(normalized)
        except Exception as error:  # noqa: BLE001
            print(f"StarDist inference failed: {error!r}")
            raise HTTPException(
                status_code=500,
                detail=f"StarDist inference failed: {error}",
            ) from error

        cells = build_cells_from_labels(labels, x, y, scale)
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)

        return {
            "roi": {
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            },
            "cellCount": len(cells),
            "cells": cells,
            "overlayType": "contours",
            "maskShape": [int(labels.shape[1]), int(labels.shape[0])],
            "model": {
                "name": f"stardist-{MODEL_NAME}",
                "percentileLow": payload.percentileLow,
                "percentileHigh": payload.percentileHigh,
                "maxDimension": payload.maxDimension,
                "scale": scale,
            },
            "source": {
                "svsUrl": payload.svsUrl,
                "cachedFilename": cached_svs_path.name,
            },
            "detailsKeys": sorted(details.keys()) if isinstance(details, dict) else [],
            "timingMs": elapsed_ms,
        }

    return web_app
