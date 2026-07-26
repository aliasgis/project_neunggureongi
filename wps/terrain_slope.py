import uuid

from raster_layers import create_terrain_product
from wps.registry import WpsResult

PROCESS = {
    "id": "terrain.slope",
    "title_ko": "경사도",
    "title_en": "Terrain slope",
    "description": "Generate a slope GeoTIFF.",
    "layer_types": ["raster"],
    "parameters": [],
    "output": "file",
}


def execute(layer, parameters, context):
    output = context["results_dir"] / f"slope_{uuid.uuid4().hex}.tif"
    create_terrain_product(layer, "slope", output, context["base_dir"])
    return WpsResult("file", output, "image/tiff", output.name)
