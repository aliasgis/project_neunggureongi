from __future__ import annotations

import io
import threading
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform_bounds
from rasterio.warp import transform as transform_coordinates

_STRETCH_CACHE: dict[tuple[str, int], tuple[float, float]] = {}
_STRETCH_LOCK = threading.RLock()


def raster_stretch_range(layer: dict, base_dir: Path) -> tuple[float, float]:
    """Return one stable display range for every tile of the same raster."""
    path = base_dir / layer["path"]
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    with _STRETCH_LOCK:
        cached = _STRETCH_CACHE.get(key)
        if cached:
            return cached
    with rasterio.open(path) as source:
        scale = min(1.0, 1024 / max(source.width, source.height))
        sample_width = max(1, round(source.width * scale))
        sample_height = max(1, round(source.height * scale))
        sample = source.read(
            1,
            out_shape=(sample_height, sample_width),
            masked=True,
            resampling=Resampling.average,
        )
        valid = sample.compressed()
        low, high = np.percentile(valid, [2, 98]) if valid.size else (0.0, 1.0)
        result = (float(low), float(high))
    with _STRETCH_LOCK:
        _STRETCH_CACHE.clear()
        _STRETCH_CACHE[key] = result
    return result


def raster_bounds4326(layer: dict, base_dir: Path) -> tuple:
    with rasterio.open(base_dir / layer["path"]) as source:
        return transform_bounds(source.crs, "EPSG:4326", *source.bounds)


def describe_raster(layer: dict, base_dir: Path) -> dict:
    with rasterio.open(base_dir / layer["path"]) as source:
        return {
            "coverageId": layer["name"],
            "crs": str(source.crs),
            "bounds": tuple(source.bounds),
            "width": source.width,
            "height": source.height,
            "bandCount": source.count,
            "bandDtypes": list(source.dtypes),
            "bandDescriptions": list(source.descriptions),
            "dtype": source.dtypes[0],
        }


def identify_raster(
    layer: dict,
    x: float,
    y: float,
    crs: str,
    base_dir: Path,
) -> dict:
    with rasterio.open(base_dir / layer["path"]) as source:
        source_x, source_y = transform_coordinates(crs, source.crs, [x], [y])
        row, column = source.index(source_x[0], source_y[0])
        if row < 0 or column < 0 or row >= source.height or column >= source.width:
            return {"type": "FeatureCollection", "features": []}
        values = source.read(window=((row, row + 1), (column, column + 1)))[:, 0, 0]
        properties = {
            "layer": layer["name"],
            "band_count": source.count,
            "pixel_row": row,
            "pixel_column": column,
        }
        valid_count = 0
        for band_index, value in enumerate(values, start=1):
            nodata = source.nodatavals[band_index - 1]
            numeric = value.item() if hasattr(value, "item") else value
            is_nodata = nodata is not None and numeric == nodata
            is_finite = not isinstance(numeric, (float, np.floating)) or np.isfinite(numeric)
            band_value = None if is_nodata or not is_finite else numeric
            if band_value is not None:
                valid_count += 1
            properties[f"band_{band_index}"] = band_value
            description = source.descriptions[band_index - 1]
            if description:
                properties[f"band_{band_index}_description"] = description
            properties[f"band_{band_index}_dtype"] = source.dtypes[band_index - 1]
        if valid_count == 0:
            return {"type": "FeatureCollection", "features": []}
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [x, y]},
                    "properties": properties,
                }
            ],
        }


def render_raster(
    layer: dict,
    bounds: tuple[float, float, float, float],
    crs: str,
    width: int,
    height: int,
    base_dir: Path,
) -> bytes:
    low, high = raster_stretch_range(layer, base_dir)
    with rasterio.open(base_dir / layer["path"]) as source:
        values = np.full((height, width), np.nan, "float32")
        reproject(
            rasterio.band(source, 1),
            values,
            src_transform=source.transform,
            src_crs=source.crs,
            dst_transform=from_bounds(*bounds, width, height),
            dst_crs=crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    valid = np.isfinite(values)
    pixels = np.zeros((height, width, 4), "uint8")
    color_map = layer.get("_sld_colormap", [])
    if color_map:
        quantities = np.array([entry["quantity"] for entry in color_map], dtype="float64")
        colors = []
        opacities = []
        for entry in color_map:
            color = entry["color"].lstrip("#")
            colors.append([int(color[index : index + 2], 16) for index in (0, 2, 4)])
            opacities.append(float(entry.get("opacity", 1)) * 255)
        colors = np.asarray(colors)
        for channel in range(3):
            pixels[..., channel] = np.interp(
                values, quantities, colors[:, channel]
            ).astype("uint8")
        pixels[..., 3] = np.where(
            valid, np.interp(values, quantities, opacities), 0
        ).astype("uint8")
        output = io.BytesIO()
        Image.fromarray(pixels).save(output, "PNG")
        return output.getvalue()
    normalized = np.clip((values - low) / max(high - low, 1e-6), 0, 1)
    pixels[..., 0] = (60 + 180 * normalized).astype("uint8")
    pixels[..., 1] = (100 + 130 * normalized).astype("uint8")
    pixels[..., 2] = (70 + 150 * normalized).astype("uint8")
    pixels[..., 3] = np.where(valid, 255, 0)
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, "PNG")
    return output.getvalue()


def create_coverage(
    layer: dict,
    bounds: tuple[float, float, float, float],
    crs: str,
    width: int,
    height: int,
    output_path: Path,
    base_dir: Path,
) -> Path:
    with rasterio.open(base_dir / layer["path"]) as source:
        nodata = source.nodata if source.nodata is not None else -9999
        values = np.full((height, width), nodata, dtype=source.dtypes[0])
        destination_transform = from_bounds(*bounds, width, height)
        reproject(
            rasterio.band(source, 1),
            values,
            src_transform=source.transform,
            src_crs=source.crs,
            dst_transform=destination_transform,
            dst_crs=crs,
            dst_nodata=nodata,
            resampling=Resampling.bilinear,
        )
        with rasterio.open(
            output_path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype=values.dtype,
            crs=crs,
            transform=destination_transform,
            nodata=nodata,
            compress="deflate",
        ) as destination:
            destination.write(values, 1)
    return output_path


def raster_statistics(layer: dict, base_dir: Path) -> dict:
    with rasterio.open(base_dir / layer["path"]) as source:
        values = source.read(1, masked=True).compressed()
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError(f'레이어 "{layer["name"]}"에 통계를 계산할 유효 픽셀이 없습니다.')
    return {
        "layer": layer["name"],
        "count": int(values.size),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values, dtype=np.float64)),
        "std": float(np.std(values, dtype=np.float64)),
    }


def create_terrain_product(
    layer: dict,
    product: str,
    output_path: Path,
    base_dir: Path,
) -> Path:
    with rasterio.open(base_dir / layer["path"]) as source:
        values = source.read(1).astype("float32")
        gradient_y, gradient_x = np.gradient(
            values, abs(source.transform.e), abs(source.transform.a)
        )
        slope = np.arctan(np.sqrt(gradient_x**2 + gradient_y**2))
        if product == "slope":
            result = np.degrees(slope)
        elif product == "hillshade":
            result = np.clip(
                255
                * (
                    np.cos(np.deg2rad(45)) * np.cos(slope)
                    + np.sin(np.deg2rad(45))
                    * np.sin(slope)
                    * np.cos(
                        np.deg2rad(315) - np.arctan2(-gradient_x, gradient_y)
                    )
                ),
                0,
                255,
            ).astype("uint8")
        else:
            raise ValueError(f"Unsupported terrain product: {product}")
        metadata = source.meta.copy()
        metadata.update(dtype=str(result.dtype), compress="deflate")
        with rasterio.open(output_path, "w", **metadata) as destination:
            destination.write(result, 1)
    return output_path
