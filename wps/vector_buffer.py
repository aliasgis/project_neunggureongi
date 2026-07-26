import uuid

from vector_layers import create_buffer
from wps.registry import WpsResult

PROCESS = {
    "id": "vector.buffer",
    "title_ko": "벡터 버퍼",
    "title_en": "Vector buffer",
    "description": "Generate a buffer GeoJSON.",
    "layer_types": ["shp", "dxf", "gpkg", "postgis"],
    "parameters": [
        {
            "name": "distance",
            "title_ko": "버퍼 거리",
            "title_en": "Buffer distance",
            "type": "number",
            "default": 10,
            "required": True,
        }
    ],
    "output": "file",
}


def execute(layer, parameters, context):
    distance = float(parameters.get("distance", 10))
    output = context["results_dir"] / f"buffer_{uuid.uuid4().hex}.geojson"
    create_buffer(
        layer,
        distance,
        output,
        context["base_dir"],
        context["db_engine"],
    )
    return WpsResult("file", output, "application/geo+json", output.name)
