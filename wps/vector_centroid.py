from wps._vector_geometry import VECTOR_TYPES, read_frame, write_geojson


PROCESS = {
    "id": "vector.centroid",
    "title_ko": "폴리곤 중심점 (Centroid)",
    "title_en": "Polygon centroid",
    "description": (
        "Create one centroid point for every Polygon or MultiPolygon feature "
        "while preserving its attributes."
    ),
    "layer_types": VECTOR_TYPES,
    "parameters": [],
    "output": "file",
}


def execute(layer, parameters, context):
    frame = read_frame(layer, context)
    if frame.empty:
        result = frame.copy()
    else:
        polygon_mask = frame.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        if not polygon_mask.all():
            invalid_types = sorted(frame.loc[~polygon_mask].geometry.geom_type.unique())
            raise ValueError(
                "vector.centroid supports Polygon and MultiPolygon only; "
                f"found: {', '.join(invalid_types)}"
            )

        result = frame.copy()
        if result.crs and result.crs.is_geographic:
            projected_crs = result.estimate_utm_crs()
            if projected_crs is None:
                raise ValueError("Could not determine a projected CRS for centroid calculation")
            projected = result.to_crs(projected_crs)
            projected.geometry = projected.geometry.centroid
            result = projected.to_crs(frame.crs)
        else:
            result.geometry = result.geometry.centroid

    result.insert(0, "wps_operation", "centroid")
    return write_geojson(result, f"centroid_{layer['name']}", context)
