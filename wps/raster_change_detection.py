from __future__ import annotations

import math

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.warp import reproject, transform_geom
from scipy.ndimage import label

from wps.registry import WpsResult


PROCESS = {
    "id": "raster.change_detection",
    "title_ko": "래스터 변화 탐지",
    "title_en": "raster change detection",
    "description": (
        "Align two rasters and detect changed pixels with an unsupervised, "
        "robust MAD anomaly model."
    ),
    "layer_types": ["raster"],
    "parameters": [
        {
            "name": "second_layer",
            "title_ko": "비교 대상 래스터",
            "title_en": "Comparison raster",
            "type": "layer",
            "layer_types": ["raster"],
            "default": "",
            "required": True,
        },
        {
            "name": "sensitivity",
            "title_ko": "탐지 민감도(낮을수록 민감)",
            "title_en": "Sensitivity (lower detects more)",
            "type": "number",
            "default": 3.5,
            "required": True,
        },
        {
            "name": "minimum_region_pixels",
            "title_ko": "최소 변화 영역(픽셀)",
            "title_en": "Minimum change region (pixels)",
            "type": "number",
            "default": 9,
            "required": True,
        },
        {
            "name": "absolute_threshold",
            "title_ko": "최소 절대 변화량(선택)",
            "title_en": "Minimum absolute change (optional)",
            "type": "number",
            "default": "",
            "required": False,
        },
    ],
    "output": "json",
}


def _remove_small_regions(mask: np.ndarray, minimum_pixels: int) -> np.ndarray:
    if minimum_pixels <= 1 or not mask.any():
        return mask
    regions, count = label(mask, structure=np.ones((3, 3), dtype="uint8"))
    if count == 0:
        return mask
    sizes = np.bincount(regions.ravel())
    keep = sizes >= minimum_pixels
    keep[0] = False
    return keep[regions]


def execute(layer, parameters, context):
    second_name = str(parameters.get("second_layer", "")).strip()
    if not second_name:
        raise ValueError("second_layer is required")
    second_layer = context["get_layer"](second_name)
    if second_layer.get("type") != "raster":
        raise ValueError("second_layer must be a raster layer")
    if second_layer["name"] == layer["name"]:
        # 상태 점검에 유용하므로 래스터를 자기 자신과 비교하는 것을 허용합니다.
        pass

    sensitivity = float(parameters.get("sensitivity", 3.5))
    if not math.isfinite(sensitivity) or sensitivity <= 0:
        raise ValueError("sensitivity must be greater than 0")
    minimum_pixels = int(float(parameters.get("minimum_region_pixels", 9)))
    if minimum_pixels < 1:
        raise ValueError("minimum_region_pixels must be at least 1")
    threshold_text = str(parameters.get("absolute_threshold", "")).strip()
    absolute_threshold = float(threshold_text) if threshold_text else None
    if absolute_threshold is not None and (
        not math.isfinite(absolute_threshold) or absolute_threshold < 0
    ):
        raise ValueError("absolute_threshold must be zero or greater")

    base_dir = context["base_dir"]
    before_path = base_dir / layer["path"]
    after_path = base_dir / second_layer["path"]
    with rasterio.open(before_path) as before_source, rasterio.open(after_path) as after_source:
        pixel_count = before_source.width * before_source.height
        if pixel_count > 50_000_000:
            raise ValueError(
                "Reference raster exceeds 50 million pixels; use a clipped raster"
            )
        before = before_source.read(1, masked=True).astype("float32").filled(np.nan)
        after = np.full(before.shape, np.nan, dtype="float32")
        reproject(
            rasterio.band(after_source, 1),
            after,
            src_transform=after_source.transform,
            src_crs=after_source.crs,
            src_nodata=after_source.nodata,
            dst_transform=before_source.transform,
            dst_crs=before_source.crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
            num_threads=2,
        )
        profile = before_source.profile.copy()

    valid = np.isfinite(before) & np.isfinite(after)
    if not valid.any():
        raise ValueError("The two rasters do not have a valid overlapping area")

    difference = after - before
    valid_difference = difference[valid].astype("float64")
    global_offset = float(np.median(valid_difference))
    anomaly = np.abs(difference - global_offset)
    valid_anomaly = anomaly[valid].astype("float64")
    anomaly_median = float(np.median(valid_anomaly))
    mad = float(np.median(np.abs(valid_anomaly - anomaly_median)))
    robust_sigma = 1.4826 * mad
    adaptive_threshold = anomaly_median + sensitivity * robust_sigma
    threshold = max(adaptive_threshold, absolute_threshold or 0.0)
    changed = valid & (anomaly > threshold)
    changed = _remove_small_regions(changed, minimum_pixels)

    regions, region_count = label(
        changed, structure=np.ones((3, 3), dtype="uint8")
    )
    if region_count > 50_000:
        raise ValueError(
            "Change result exceeds 50,000 polygons; increase sensitivity or "
            "minimum_region_pixels"
        )

    transform = profile["transform"]
    source_crs = profile["crs"]
    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)
    features = []
    for geometry, region_value in shapes(
        regions.astype("int32"),
        mask=changed,
        transform=transform,
        connectivity=8,
    ):
        region_id = int(region_value)
        region_mask = regions == region_id
        region_difference = difference[region_mask].astype("float64")
        region_anomaly = anomaly[region_mask].astype("float64")
        geometry4326 = transform_geom(
            source_crs, "EPSG:4326", geometry, precision=7
        )
        features.append(
            {
                "type": "Feature",
                "id": region_id,
                "geometry": geometry4326,
                "properties": {
                    "region_id": region_id,
                    "pixel_count": int(region_mask.sum()),
                    "area_native": float(region_mask.sum() * pixel_area),
                    "mean_change": float(np.mean(region_difference)),
                    "mean_abs_change": float(np.mean(np.abs(region_difference))),
                    "max_abs_change": float(np.max(np.abs(region_difference))),
                    "max_anomaly_score": float(np.max(region_anomaly)),
                    "reference_layer": layer["name"],
                    "comparison_layer": second_layer["name"],
                },
            }
        )

    result = {
        "type": "FeatureCollection",
        "name": "raster_change_detection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "analysis": {
            "model": "unsupervised_robust_mad",
            "reference_layer": layer["name"],
            "comparison_layer": second_layer["name"],
            "sensitivity": sensitivity,
            "global_offset": global_offset,
            "robust_sigma": robust_sigma,
            "adaptive_threshold": adaptive_threshold,
            "applied_threshold": threshold,
            "minimum_region_pixels": minimum_pixels,
            "valid_pixels": int(valid.sum()),
            "changed_pixels": int(changed.sum()),
            "polygon_count": len(features),
            "source_crs": str(source_crs),
        },
        "features": features,
    }
    return WpsResult("json", result)
