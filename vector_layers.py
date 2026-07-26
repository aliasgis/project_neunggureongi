from __future__ import annotations

import io
import json
import math
import hashlib
import threading
from pathlib import Path
from typing import Callable

import ezdxf
import geopandas as gpd
import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer
from pyogrio import list_layers, read_info
from shapely.geometry import LineString, Point, Polygon, box
from sqlalchemy import text

_INDEX_LOCK = threading.RLock()


def list_geopackage_layers(path: Path) -> list[dict]:
    return [
        {"name": str(name), "geometry_type": str(geometry_type or "")}
        for name, geometry_type in list_layers(path)
    ]


def _vector_index_paths(layer: dict, base_dir: Path) -> tuple[Path, Path]:
    source = (base_dir / layer["path"]).resolve()
    cache_dir = base_dir / "cache" / "vector_index"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(source).lower().encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.fgb", cache_dir / f"{key}.json"


def get_vector_spatial_index(layer: dict, base_dir: Path) -> Path | None:
    if layer.get("type") != "shp":
        return None
    source = (base_dir / layer["path"]).resolve()
    index_path, metadata_path = _vector_index_paths(layer, base_dir)
    if not source.exists() or not index_path.exists() or not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        stat = source.stat()
        if (
            metadata.get("source") == str(source)
            and metadata.get("mtime_ns") == stat.st_mtime_ns
            and metadata.get("size") == stat.st_size
            and metadata.get("attributes") is True
        ):
            return index_path
    except (OSError, ValueError, TypeError):
        pass
    return None


def build_vector_spatial_index(
    layer: dict, base_dir: Path, force: bool = False
) -> dict:
    if layer.get("type") != "shp":
        return {"status": "skipped", "reason": "not a shapefile"}
    source = (base_dir / layer["path"]).resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    with _INDEX_LOCK:
        existing = None if force else get_vector_spatial_index(layer, base_dir)
        if existing:
            return {"status": "ready", "path": str(existing), "cached": True}
        index_path, metadata_path = _vector_index_paths(layer, base_dir)
        temporary = index_path.with_name(index_path.stem + ".building.fgb")
        temporary.unlink(missing_ok=True)
        frame = gpd.read_file(source)
        frame.to_file(temporary, driver="FlatGeobuf", SPATIAL_INDEX="YES")
        temporary.replace(index_path)
        stat = source.stat()
        metadata = {
            "source": str(source),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "features": len(frame),
            "crs": str(frame.crs or layer.get("crs", "")),
            "attributes": True,
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "status": "created",
            "path": str(index_path),
            "features": len(frame),
            "cached": False,
        }


def _rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = value.replace("#", "").replace("0x", "")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4)) + (alpha,)


def read_dxf_features(layer: dict, base_dir: Path) -> list[tuple[object, dict]]:
    document = ezdxf.readfile(base_dir / layer["path"])
    features = []
    for entity in document.modelspace():
        entity_type = entity.dxftype()
        geometry = None
        properties = {
            "entity_type": entity_type,
            "layer": getattr(entity.dxf, "layer", None),
        }
        if entity_type == "LINE":
            geometry = LineString(
                [
                    (entity.dxf.start.x, entity.dxf.start.y),
                    (entity.dxf.end.x, entity.dxf.end.y),
                ]
            )
        elif entity_type == "LWPOLYLINE":
            points = [(value[0], value[1]) for value in entity.get_points()]
            geometry = (
                Polygon(points)
                if entity.closed and len(points) >= 3
                else LineString(points)
            )
        elif entity_type == "POINT":
            geometry = Point(entity.dxf.location.x, entity.dxf.location.y)
        elif entity_type == "CIRCLE":
            geometry = Point(entity.dxf.center.x, entity.dxf.center.y).buffer(
                float(entity.dxf.radius), 32
            )
        elif entity_type == "ARC":
            center = entity.dxf.center
            radius = float(entity.dxf.radius)
            start = math.radians(float(entity.dxf.start_angle))
            end = math.radians(float(entity.dxf.end_angle))
            if end < start:
                end += 2 * math.pi
            geometry = LineString(
                [
                    (
                        center.x + radius * math.cos(angle),
                        center.y + radius * math.sin(angle),
                    )
                    for angle in np.linspace(start, end, 64)
                ]
            )
        elif entity_type in ("TEXT", "MTEXT"):
            insert = entity.dxf.insert
            geometry = Point(insert.x, insert.y)
            properties["text"] = (
                entity.plain_text() if entity_type == "MTEXT" else entity.dxf.text
            )
        if geometry is not None:
            features.append((geometry, properties))
    return features


def read_vector_layer(
    layer: dict,
    base_dir: Path,
    engine_factory: Callable,
    target_crs: str | None = None,
    bbox_filter: tuple[float, float, float, float] | None = None,
    limit: int | None = None,
    geometry_only: bool = False,
) -> gpd.GeoDataFrame:
    if layer["type"] == "shp":
        source_path = get_vector_spatial_index(layer, base_dir) or (
            base_dir / layer["path"]
        )
        frame = gpd.read_file(
            source_path,
            bbox=bbox_filter,
            columns=[] if geometry_only else None,
            max_features=limit,
        )
        if frame.crs is None:
            frame = frame.set_crs(layer["crs"])
    elif layer["type"] == "gpkg":
        read_options = {
            "bbox": bbox_filter,
            "columns": [] if geometry_only else None,
            "max_features": limit,
        }
        if layer.get("table"):
            read_options["layer"] = layer["table"]
        frame = gpd.read_file(base_dir / layer["path"], **read_options)
        if frame.crs is None:
            frame = frame.set_crs(layer["crs"])
    elif layer["type"] == "dxf":
        features = read_dxf_features(layer, base_dir)
        frame = gpd.GeoDataFrame(
            [properties for _, properties in features],
            geometry=[geometry for geometry, _ in features],
            crs=layer["crs"],
        )
    elif layer["type"] == "postgis":
        schema = layer.get("schema", "public")
        table = layer["table"]
        geometry_column = layer.get("geometry_column", "geom")
        columns = layer.get("columns", "*")
        query = f'SELECT {columns} FROM "{schema}"."{table}"'
        params = {}
        if bbox_filter:
            minx, miny, maxx, maxy = bbox_filter
            srid = int(str(layer.get("crs", "EPSG:4326")).split(":")[-1])
            query += (
                f' WHERE ST_Intersects("{geometry_column}", '
                "ST_MakeEnvelope(:minx,:miny,:maxx,:maxy,:srid))"
            )
            params = {
                "minx": minx,
                "miny": miny,
                "maxx": maxx,
                "maxy": maxy,
                "srid": srid,
            }
        if limit:
            query += " LIMIT :limit"
            params["limit"] = int(limit)
        frame = gpd.read_postgis(
            text(query),
            engine_factory(),
            geom_col=geometry_column,
            params=params,
        )
        if frame.crs is None:
            frame = frame.set_crs(layer.get("crs", "EPSG:4326"))
    else:
        raise ValueError(f'Unsupported vector type: {layer["type"]}')
    if bbox_filter and layer["type"] == "dxf":
        frame = frame[frame.intersects(box(*bbox_filter))]
    return frame.to_crs(target_crs) if target_crs else frame


def vector_bounds4326(layer: dict, base_dir: Path, engine_factory: Callable) -> tuple:
    if layer["type"] == "shp":
        info = read_info(base_dir / layer["path"])
        bounds = tuple(info["total_bounds"])
        source_crs = info.get("crs") or layer.get("crs", "EPSG:4326")
        if str(source_crs).upper() == "EPSG:4326":
            return bounds
        transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
        minx, miny, maxx, maxy = bounds
        corners = [
            transformer.transform(minx, miny),
            transformer.transform(minx, maxy),
            transformer.transform(maxx, miny),
            transformer.transform(maxx, maxy),
        ]
        return (
            min(point[0] for point in corners),
            min(point[1] for point in corners),
            max(point[0] for point in corners),
            max(point[1] for point in corners),
        )
    return tuple(
        read_vector_layer(layer, base_dir, engine_factory, "EPSG:4326").total_bounds
    )


def identify_vector(
    layer: dict,
    x: float,
    y: float,
    tolerance_x: float,
    tolerance_y: float,
    crs: str,
    base_dir: Path,
    engine_factory: Callable,
    limit: int = 10,
) -> dict:
    frame = read_vector_layer(layer, base_dir, engine_factory, crs)
    search_area = box(
        x - tolerance_x, y - tolerance_y, x + tolerance_x, y + tolerance_y
    )
    matches = frame[frame.intersects(search_area)].head(limit)
    if matches.empty:
        return {"type": "FeatureCollection", "features": []}
    return json.loads(matches.to_json())


def render_vector(
    layer: dict,
    bounds: tuple[float, float, float, float],
    crs: str,
    width: int,
    height: int,
    base_dir: Path,
    engine_factory: Callable,
) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    minx, miny, maxx, maxy = bounds
    if str(layer.get("crs", crs)).upper() != str(crs).upper():
        transformer = Transformer.from_crs(crs, layer["crs"], always_xy=True)
        corners = [
            transformer.transform(minx, miny),
            transformer.transform(minx, maxy),
            transformer.transform(maxx, miny),
            transformer.transform(maxx, maxy),
        ]
        source_bounds = (
            min(point[0] for point in corners),
            min(point[1] for point in corners),
            max(point[0] for point in corners),
            max(point[1] for point in corners),
        )
    else:
        source_bounds = bounds
    render_layer = layer
    spatial_index = get_vector_spatial_index(layer, base_dir)
    if spatial_index:
        render_layer = {**layer, "path": spatial_index.relative_to(base_dir).as_posix()}
    frame = read_vector_layer(
        render_layer,
        base_dir,
        engine_factory,
        None,
        bbox_filter=source_bounds,
        geometry_only=True,
    )
    max_render_features = 50000
    if len(frame) > max_render_features:
        step = math.ceil(len(frame) / max_render_features)
        frame = frame.iloc[::step].copy()
    draw_minx, draw_miny, draw_maxx, draw_maxy = source_bounds
    tolerance = max(
        (draw_maxx - draw_minx) / width,
        (draw_maxy - draw_miny) / height,
    ) * 0.35
    if tolerance > 0 and not frame.empty:
        frame.geometry = frame.geometry.simplify(tolerance, preserve_topology=True)

    def pixel(x, y):
        return (
            (x - draw_minx) / (draw_maxx - draw_minx) * width,
            (draw_maxy - y) / (draw_maxy - draw_miny) * height,
        )

    style = layer.get("style", {})
    stroke = _rgba(style.get("stroke", "#0055cc"))
    opacity = int(float(style.get("fill_opacity", 0.27)) * 255)
    fill = _rgba(style.get("fill", "#66aaff"), opacity)
    stroke_width = max(1, round(float(style.get("stroke_width", 2))))
    point_radius = max(2, float(style.get("point_size", 8)) / 2)
    def draw_geometry(geometry):
        if geometry is None or geometry.is_empty:
            return
        if geometry.geom_type.startswith("Multi") or geometry.geom_type == "GeometryCollection":
            for part in geometry.geoms:
                draw_geometry(part)
            return
        if geometry.geom_type == "Point":
            x, y = pixel(geometry.x, geometry.y)
            draw.ellipse(
                (x - point_radius, y - point_radius, x + point_radius, y + point_radius),
                fill=fill,
                outline=stroke,
                width=stroke_width,
            )
        elif geometry.geom_type == "LineString":
            draw.line(
                [pixel(x, y) for x, y in geometry.coords],
                fill=stroke,
                width=stroke_width,
            )
        elif geometry.geom_type == "Polygon":
            draw.polygon(
                [pixel(x, y) for x, y in geometry.exterior.coords],
                fill=fill,
                outline=stroke,
            )
    for geometry in frame.loc[frame.intersects(box(*source_bounds)), "geometry"]:
        draw_geometry(geometry)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def create_buffer(
    layer: dict,
    distance: float,
    output_path: Path,
    base_dir: Path,
    engine_factory: Callable,
) -> Path:
    frame = read_vector_layer(layer, base_dir, engine_factory)
    frame.geometry = frame.buffer(distance)
    frame.to_file(output_path, driver="GeoJSON")
    return output_path
