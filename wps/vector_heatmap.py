import math
import re
import uuid

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from scipy.ndimage import distance_transform_edt, gaussian_filter

from vector_layers import read_vector_layer
from wps.registry import WpsResult

try:
    import cupy as cp
    from cupyx.scipy.ndimage import gaussian_filter as gpu_gaussian_filter
except (ImportError, ModuleNotFoundError):
    cp = None
    gpu_gaussian_filter = None


PROCESS = {
    "id": "vector.heatmap",
    "title_ko": "벡터 히트맵",
    "title_en": "Vector heatmap",
    "description": "Create a density heatmap GeoTIFF in EPSG:3857 from vector feature centroids.",
    "layer_types": ["shp", "dxf", "gpkg", "postgis"],
    "parameters": [
        {
            "name": "radius_m",
            "title_ko": "영향 반경(m)",
            "title_en": "Radius (m)",
            "type": "number",
            "default": 1000,
            "required": True,
        },
        {
            "name": "pixel_size_m",
            "title_ko": "픽셀 크기(m)",
            "title_en": "Pixel size (m)",
            "type": "number",
            "default": 100,
            "required": True,
        },
        {
            "name": "weight_field",
            "title_ko": "가중치 필드(선택)",
            "title_en": "Weight field (optional)",
            "type": "text",
            "default": "",
            "required": False,
        },
        {
            "name": "low_color",
            "title_ko": "낮은 밀도 색상",
            "title_en": "Low density color",
            "type": "color",
            "default": "#2c7bb6",
            "required": True,
        },
        {
            "name": "high_color",
            "title_ko": "높은 밀도 색상",
            "title_en": "High density color",
            "type": "color",
            "default": "#d7191c",
            "required": True,
        },
    ],
    "output": "file",
}


def apply_gaussian_filter(density, sigma):
    """Use CUDA when available and fall back to SciPy on any GPU runtime error."""
    if cp is not None and gpu_gaussian_filter is not None:
        try:
            device_count = cp.cuda.runtime.getDeviceCount()
            if device_count > 0:
                gpu_density = cp.asarray(density)
                gpu_result = gpu_gaussian_filter(gpu_density, sigma=sigma, mode="constant")
                result = cp.asnumpy(gpu_result)
                del gpu_result, gpu_density
                return result, "cupy-cuda"
        except Exception:
            pass
    return gaussian_filter(density, sigma=sigma, mode="constant"), "scipy-cpu"


def execute(layer, parameters, context):
    radius = float(parameters.get("radius_m", 1000))
    pixel_size = float(parameters.get("pixel_size_m", 100))
    weight_field = str(parameters.get("weight_field", "")).strip()
    low_color = str(parameters.get("low_color", "#2c7bb6")).strip()
    high_color = str(parameters.get("high_color", "#d7191c")).strip()
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("radius_m must be greater than 0")
    if not math.isfinite(pixel_size) or pixel_size <= 0:
        raise ValueError("pixel_size_m must be greater than 0")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", low_color):
        raise ValueError("low_color must be a hex color such as #2c7bb6")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", high_color):
        raise ValueError("high_color must be a hex color such as #d7191c")

    frame = read_vector_layer(
        layer,
        context["base_dir"],
        context["db_engine"],
        target_crs="EPSG:3857",
    )
    frame = frame.loc[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    if frame.empty:
        raise ValueError("The layer has no valid geometry")

    points = frame.geometry.copy()
    non_points = ~points.geom_type.isin(["Point", "MultiPoint"])
    if non_points.any():
        points.loc[non_points] = points.loc[non_points].centroid
    points = points.explode(index_parts=False)
    x = points.x.to_numpy(dtype="float64")
    y = points.y.to_numpy(dtype="float64")

    if weight_field:
        if weight_field not in frame.columns:
            raise ValueError(f'Unknown weight field: {weight_field}')
        numeric = np.asarray(frame[weight_field], dtype="float64")
        if len(numeric) != len(x):
            raise ValueError("MultiPoint layers cannot use weight_field")
        if not np.isfinite(numeric).all() or (numeric < 0).any():
            raise ValueError("weight_field must contain finite, non-negative numbers")
        weights = numeric
    else:
        weights = np.ones(len(x), dtype="float64")

    padding = max(radius * 3, pixel_size)
    minx, miny, maxx, maxy = x.min() - padding, y.min() - padding, x.max() + padding, y.max() + padding
    width = max(1, int(math.ceil((maxx - minx) / pixel_size)))
    height = max(1, int(math.ceil((maxy - miny) / pixel_size)))
    if width * height > 16_000_000:
        raise ValueError("Heatmap exceeds 16 million pixels; increase pixel_size_m")

    density, _, _ = np.histogram2d(y, x, bins=[height, width], range=[[miny, maxy], [minx, maxx]], weights=weights)
    occupied = density > 0
    density, accelerator = apply_gaussian_filter(density, radius / pixel_size)
    # Gaussian tails are positive across the rectangular raster and would make
    # WMS render the whole bounding box. Keep a circular 3-sigma influence
    # area around source cells and write everything outside it as NoData (0).
    influence_distance_m = distance_transform_edt(~occupied) * pixel_size
    density[influence_distance_m > radius * 3] = 0
    density = np.flipud(density).astype("float32")
    output = context["results_dir"] / f"heatmap_{uuid.uuid4().hex}.tif"
    with rasterio.open(
        output,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        crs="EPSG:3857",
        transform=from_bounds(minx, miny, maxx, maxy, width, height),
        nodata=0,
        compress="deflate",
        tiled=width >= 256 and height >= 256,
    ) as destination:
        destination.write(density, 1)
        destination.set_band_description(1, "heat_density")
        destination.update_tags(
            process="vector.heatmap",
            source_layer=layer["name"],
            radius_m=radius,
            pixel_size_m=pixel_size,
            weight_field=weight_field,
            low_color=low_color,
            high_color=high_color,
            accelerator=accelerator,
        )
    return WpsResult("file", output, "image/tiff", output.name)
