"""Copy this file, remove the leading underscore, and implement execute()."""

from wps.registry import WpsResult

PROCESS = {
    "id": "category.algorithm_name",
    "title_ko": "알고리즘 이름",
    "title_en": "Algorithm name",
    "description": "Describe what this algorithm does.",
    "layer_types": ["raster"],  # raster, shp, dxf, gpkg, postgis
    "parameters": [
        {
            "name": "example_value",
            "title_ko": "예제 값",
            "title_en": "Example value",
            "type": "number",
            "default": 10,
            "required": True,
        }
    ],
    "output": "json",  # json or file
}


def execute(layer, parameters, context):
    value = float(parameters.get("example_value", 10))
    return WpsResult(
        "json",
        {
            "layer": layer["name"],
            "example_value": value,
            "message": "Replace this function with the algorithm implementation.",
        },
    )
