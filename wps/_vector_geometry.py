from __future__ import annotations

import uuid

import geopandas as gpd

from vector_layers import read_vector_layer
from wps.registry import WpsResult


VECTOR_TYPES = ["shp", "dxf", "gpkg", "postgis"]
MAX_FEATURES = 100_000


def read_frame(layer, context):
    frame = read_vector_layer(
        layer,
        context["base_dir"],
        context["db_engine"],
        limit=MAX_FEATURES + 1,
    )
    if len(frame) > MAX_FEATURES:
        raise ValueError(f"Vector operation is limited to {MAX_FEATURES:,} features per layer")
    if frame.crs is None:
        frame = frame.set_crs(layer.get("crs", "EPSG:4326"))
    frame = frame.loc[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    return frame


def write_geojson(frame: gpd.GeoDataFrame, prefix: str, context):
    output = context["results_dir"] / f"{prefix}_{uuid.uuid4().hex}.geojson"
    frame.to_file(output, driver="GeoJSON")
    return WpsResult("file", output, "application/geo+json", output.name)


def execute_binary_predicate(layer, parameters, context, predicate: str):
    comparison_name = str(parameters.get("comparison_layer", "")).strip()
    if not comparison_name:
        raise ValueError("comparison_layer is required")
    comparison_layer = context["get_layer"](comparison_name)
    if comparison_layer.get("type") not in VECTOR_TYPES:
        raise ValueError("comparison_layer must be a vector layer")

    source = read_frame(layer, context).reset_index(drop=True)
    comparison = read_frame(comparison_layer, context)
    if source.empty or comparison.empty:
        result = source.iloc[0:0].copy()
    else:
        comparison = comparison.to_crs(source.crs)
        join_predicate = "intersects" if predicate == "disjoint" else predicate
        joined = gpd.sjoin(
            source[[source.geometry.name]],
            comparison[[comparison.geometry.name]],
            how="inner",
            predicate=join_predicate,
        )
        matched_indices = joined.index.unique().sort_values()
        if predicate == "disjoint":
            result = source.loc[~source.index.isin(matched_indices)].copy()
        else:
            result = source.loc[matched_indices].copy()

    result.insert(0, "wps_predicate", predicate)
    result.insert(1, "comparison_layer", comparison_layer["name"])
    return write_geojson(result, f"{predicate}_{layer['name']}", context)


def binary_process(process_id: str, title_ko: str, title_en: str, predicate: str):
    return {
        "id": process_id,
        "title_ko": title_ko,
        "title_en": title_en,
        "description": (
            f"Select input features for which the {predicate} predicate is true "
            "against at least one feature in the comparison layer."
        ),
        "layer_types": VECTOR_TYPES,
        "parameters": [
            {
                "name": "comparison_layer",
                "title_ko": "비교 벡터 레이어",
                "title_en": "Comparison vector layer",
                "type": "layer",
                "layer_types": VECTOR_TYPES,
                "default": "",
                "required": True,
            }
        ],
        "output": "file",
    }
