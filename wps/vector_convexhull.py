import geopandas as gpd

from wps._vector_geometry import VECTOR_TYPES, read_frame, write_geojson


PROCESS = {
    "id": "vector.convexhull",
    "title_ko": "벡터 볼록껍질 (Convex hull)",
    "title_en": "Vector convex hull",
    "description": "Create one hull for the whole layer or one hull per feature.",
    "layer_types": VECTOR_TYPES,
    "parameters": [
        {
            "name": "mode",
            "title_ko": "생성 방식 (whole 또는 per_feature)",
            "title_en": "Mode (whole or per_feature)",
            "type": "text",
            "default": "whole",
            "required": True,
        }
    ],
    "output": "file",
}


def execute(layer, parameters, context):
    mode = str(parameters.get("mode", "whole")).strip().lower()
    if mode not in {"whole", "per_feature"}:
        raise ValueError("mode must be whole or per_feature")
    source = read_frame(layer, context)
    if source.empty:
        raise ValueError("Input layer has no non-empty geometries")

    if mode == "whole":
        geometry = source.geometry.union_all().convex_hull
        result = gpd.GeoDataFrame(
            {
                "source_layer": [layer["name"]],
                "source_feature_count": [len(source)],
                "mode": [mode],
            },
            geometry=[geometry],
            crs=source.crs,
        )
    else:
        result = source.copy()
        result.geometry = result.geometry.convex_hull
        result.insert(0, "wps_mode", mode)

    return write_geojson(result, f"convexhull_{layer['name']}", context)
