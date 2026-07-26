from raster_layers import raster_statistics
from wps.registry import WpsResult

PROCESS = {
    "id": "raster.statistics",
    "title_ko": "래스터 통계",
    "title_en": "Raster statistics",
    "description": "Minimum, maximum, mean and standard deviation.",
    "layer_types": ["raster"],
    "parameters": [],
    "output": "json",
}


def execute(layer, parameters, context):
    return WpsResult("json", raster_statistics(layer, context["base_dir"]))
