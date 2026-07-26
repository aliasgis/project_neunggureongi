import uuid

from raster_layers import create_terrain_product
from wps.registry import WpsResult

PROCESS = {
    "id": "terrain.hillshade",
    "title_ko": "음영기복",
    "title_en": "Terrain hillshade",
    "description": "Generate a hillshade GeoTIFF.",
    "layer_types": ["raster"],
    "parameters": [],
    "output": "file",
}


def execute(layer, parameters, context):
    output = context["results_dir"] / f"hillshade_{uuid.uuid4().hex}.tif"
    create_terrain_product(layer, "hillshade", output, context["base_dir"])
    return WpsResult("file", output, "image/tiff", output.name)
