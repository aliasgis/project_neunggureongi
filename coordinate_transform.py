from __future__ import annotations

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError


MAX_COORDINATES = 10_000


def transform_coordinates(
    source_crs: str,
    target_crs: str,
    coordinates: list[list[float]],
) -> dict:
    if not coordinates:
        raise ValueError("변환할 좌표가 없습니다.")
    if len(coordinates) > MAX_COORDINATES:
        raise ValueError(f"한 번에 최대 {MAX_COORDINATES:,}개 좌표를 변환할 수 있습니다.")
    try:
        source = CRS.from_user_input(source_crs)
        target = CRS.from_user_input(target_crs)
        transformer = Transformer.from_crs(source, target, always_xy=True)
    except CRSError as error:
        raise ValueError(f"좌표계를 해석할 수 없습니다: {error}") from error

    dimensions = {len(coordinate) for coordinate in coordinates}
    if not dimensions.issubset({2, 3}):
        raise ValueError("각 좌표는 [X, Y] 또는 [X, Y, Z] 형식이어야 합니다.")
    if len(dimensions) != 1:
        raise ValueError("2차원 좌표와 3차원 좌표를 한 요청에 섞을 수 없습니다.")
    try:
        x_values = [float(coordinate[0]) for coordinate in coordinates]
        y_values = [float(coordinate[1]) for coordinate in coordinates]
        if dimensions == {3}:
            z_values = [float(coordinate[2]) for coordinate in coordinates]
            transformed = transformer.transform(x_values, y_values, z_values)
            output = [
                [float(x), float(y), float(z)]
                for x, y, z in zip(*transformed)
            ]
        else:
            transformed = transformer.transform(x_values, y_values)
            output = [
                [float(x), float(y)]
                for x, y in zip(*transformed)
            ]
    except (TypeError, ValueError, ProjError) as error:
        raise ValueError(f"좌표변환에 실패했습니다: {error}") from error
    return {
        "source_crs": source.to_string(),
        "target_crs": target.to_string(),
        "count": len(output),
        "coordinates": output,
    }
