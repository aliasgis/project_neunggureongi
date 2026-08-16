from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from wps.registry import WpsResult


PROCESS = {
    "id": "terrain.change_forecast",
    "title_ko": "지형 변화 2026·2027 예측",
    "title_en": "Terrain change forecast for 2026 and 2027",
    "description": (
        "Use CHANGE score point data, SciPy spatial-neighbour features and a "
        "Random Forest regressor to forecast 2026 and 2027 scores."
    ),
    "requires_layer": False,
    "layer_types": [],
    "parameters": [
        {
            "name": "data_file",
            "title_ko": "CHANGE CSV 파일명",
            "title_en": "CHANGE CSV filename",
            "type": "text",
            "default": "JEJU_TERRAIN_CHANGE_SCORE_POINTS_2021_2025_SAFE.csv",
            "required": True,
        },
        {
            "name": "n_estimators",
            "title_ko": "Random Forest 트리 수",
            "title_en": "Random Forest trees",
            "type": "number",
            "default": 300,
            "required": True,
        },
        {
            "name": "neighbors",
            "title_ko": "공간 이웃 지점 수",
            "title_en": "Spatial neighbours",
            "type": "number",
            "default": 8,
            "required": True,
        },
        {
            "name": "random_state",
            "title_ko": "난수 시드",
            "title_en": "Random seed",
            "type": "number",
            "default": 42,
            "required": True,
        },
    ],
    "output": "json",
}


YEARS = np.arange(2021, 2026, dtype="int16")
SCORE_COLUMNS = [f"score_{year}" for year in YEARS]


def _input_path(base_dir: Path, filename: str) -> Path:
    data_dir = (base_dir / "data").resolve()
    if not filename or Path(filename).name != filename:
        raise ValueError("data_file must be a filename inside the data folder")
    path = (data_dir / filename).resolve()
    if path.parent != data_dir or "change" not in path.name.lower():
        raise ValueError("data_file must be a CHANGE CSV inside the data folder")
    if not path.is_file() or path.suffix.lower() != ".csv":
        raise ValueError(f"CHANGE CSV was not found: {filename}")
    return path


def _read_change_csv(path: Path):
    point_ids: list[str] = []
    coordinates: list[tuple[float, float]] = []
    scores: list[list[float]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"point_id", "longitude", "latitude", *SCORE_COLUMNS}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CHANGE CSV columns are missing: {', '.join(sorted(missing))}")
        for line_number, row in enumerate(reader, start=2):
            try:
                point_id = str(row["point_id"]).strip()
                longitude = float(row["longitude"])
                latitude = float(row["latitude"])
                values = [float(row[column]) for column in SCORE_COLUMNS]
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid value in CHANGE CSV row {line_number}") from error
            if not point_id or not all(math.isfinite(value) for value in [longitude, latitude, *values]):
                raise ValueError(f"Missing or non-finite value in CHANGE CSV row {line_number}")
            point_ids.append(point_id)
            coordinates.append((longitude, latitude))
            scores.append(values)
    if len(point_ids) < 20:
        raise ValueError("CHANGE CSV requires at least 20 points")
    return point_ids, np.asarray(coordinates, dtype="float64"), np.asarray(scores, dtype="float64")


def _local_means(tree: cKDTree, values: np.ndarray, neighbors: int) -> np.ndarray:
    # 모든 점은 자기 자신의 최근접 이웃이므로 점을 하나 더 조회합니다.
    _, indices = tree.query(tree.data, k=min(neighbors + 1, len(values)))
    if indices.ndim == 1:
        indices = indices[:, None]
    neighbour_indices = indices[:, 1:] if indices.shape[1] > 1 else indices
    return np.mean(values[neighbour_indices], axis=1)


def _features(
    coordinates: np.ndarray,
    target_year: int,
    previous: np.ndarray,
    previous_delta: np.ndarray,
    local_previous: np.ndarray,
) -> np.ndarray:
    return np.column_stack(
        (
            coordinates[:, 0],
            coordinates[:, 1],
            np.full(len(coordinates), target_year, dtype="float64"),
            previous,
            previous_delta,
            local_previous,
        )
    )


def _training_rows(coordinates, scores, tree, neighbors, target_years):
    rows = []
    targets = []
    for target_year in target_years:
        target_index = int(target_year - YEARS[0])
        previous = scores[:, target_index - 1]
        previous_delta = (
            previous - scores[:, target_index - 2]
            if target_index >= 2
            else np.zeros(len(scores), dtype="float64")
        )
        local_previous = _local_means(tree, previous, neighbors)
        rows.append(
            _features(
                coordinates,
                int(target_year),
                previous,
                previous_delta,
                local_previous,
            )
        )
        targets.append(scores[:, target_index])
    return np.vstack(rows), np.concatenate(targets)


def _forest(n_estimators: int, random_state: int):
    try:
        from sklearn.ensemble import RandomForestRegressor
    except ImportError as error:
        raise RuntimeError(
            "scikit-learn is required; reinstall the server with updated requirements.txt"
        ) from error
    return RandomForestRegressor(
        n_estimators=n_estimators,
        min_samples_leaf=2,
        max_features=0.8,
        n_jobs=-1,
        random_state=random_state,
    )


def _predict_with_uncertainty(model, features):
    predictions = np.asarray([tree.predict(features) for tree in model.estimators_])
    return predictions.mean(axis=0), predictions.std(axis=0)


def execute(layer, parameters, context):
    filename = str(parameters.get("data_file", PROCESS["parameters"][0]["default"])).strip()
    n_estimators = int(float(parameters.get("n_estimators", 300)))
    neighbors = int(float(parameters.get("neighbors", 8)))
    random_state = int(float(parameters.get("random_state", 42)))
    if not 50 <= n_estimators <= 2000:
        raise ValueError("n_estimators must be between 50 and 2000")
    if not 1 <= neighbors <= 100:
        raise ValueError("neighbors must be between 1 and 100")

    source_path = _input_path(Path(context["base_dir"]), filename)
    point_ids, coordinates, scores = _read_change_csv(source_path)
    tree = cKDTree(coordinates)

    # 엄격한 시간순 백테스트: 2022~2024년 전이 데이터만 검증 모델 학습에 사용하고,
    # 따로 보관한 2025년 관측값으로 1년 예측 품질을 평가합니다.
    validation_x, validation_y = _training_rows(
        coordinates, scores, tree, neighbors, range(2022, 2025)
    )
    validation_model = _forest(n_estimators, random_state)
    validation_model.fit(validation_x, validation_y)
    score_2024 = scores[:, 3]
    validation_features = _features(
        coordinates,
        2025,
        score_2024,
        score_2024 - scores[:, 2],
        _local_means(tree, score_2024, neighbors),
    )
    held_out_prediction = validation_model.predict(validation_features)
    held_out_error = held_out_prediction - scores[:, 4]

    train_x, train_y = _training_rows(
        coordinates, scores, tree, neighbors, range(2022, 2026)
    )
    model = _forest(n_estimators, random_state)
    model.fit(train_x, train_y)

    observed_min = float(np.min(scores))
    observed_max = float(np.max(scores))
    score_2025 = scores[:, 4]
    features_2026 = _features(
        coordinates,
        2026,
        score_2025,
        score_2025 - scores[:, 3],
        _local_means(tree, score_2025, neighbors),
    )
    prediction_2026, uncertainty_2026 = _predict_with_uncertainty(model, features_2026)
    prediction_2026 = np.clip(prediction_2026, observed_min, observed_max)

    features_2027 = _features(
        coordinates,
        2027,
        prediction_2026,
        prediction_2026 - score_2025,
        _local_means(tree, prediction_2026, neighbors),
    )
    prediction_2027, uncertainty_2027 = _predict_with_uncertainty(model, features_2027)
    prediction_2027 = np.clip(prediction_2027, observed_min, observed_max)

    predictions = []
    for index, point_id in enumerate(point_ids):
        predictions.append(
            {
                "point_id": point_id,
                "longitude": round(float(coordinates[index, 0]), 8),
                "latitude": round(float(coordinates[index, 1]), 8),
                "score_2025": round(float(score_2025[index]), 4),
                "score_2026": round(float(prediction_2026[index]), 4),
                "change_2026": round(float(prediction_2026[index] - score_2025[index]), 4),
                "uncertainty_2026": round(float(uncertainty_2026[index]), 4),
                "score_2027": round(float(prediction_2027[index]), 4),
                "change_2027": round(float(prediction_2027[index] - prediction_2026[index]), 4),
                "uncertainty_2027": round(float(uncertainty_2027[index]), 4),
            }
        )

    return WpsResult(
        "json",
        {
            "status": "completed",
            "process": PROCESS["id"],
            "source": f"data/{filename}",
            "model": {
                "algorithm": "scikit-learn RandomForestRegressor",
                "spatial_features": "SciPy cKDTree neighbour mean",
                "training_years": [2021, 2022, 2023, 2024, 2025],
                "forecast_years": [2026, 2027],
                "n_estimators": n_estimators,
                "neighbors": neighbors,
                "random_state": random_state,
                "validation": {
                    "method": "train through 2024, hold out 2025",
                    "mae": round(float(np.mean(np.abs(held_out_error))), 6),
                    "rmse": round(float(np.sqrt(np.mean(held_out_error**2))), 6),
                },
            },
            "summary": {
                "point_count": len(predictions),
                "mean_score_2025": round(float(np.mean(score_2025)), 6),
                "mean_score_2026": round(float(np.mean(prediction_2026)), 6),
                "mean_change_2026": round(float(np.mean(prediction_2026 - score_2025)), 6),
                "mean_score_2027": round(float(np.mean(prediction_2027)), 6),
                "mean_change_2027": round(float(np.mean(prediction_2027 - prediction_2026)), 6),
            },
            "predictions": predictions,
        },
    )
