from __future__ import annotations

import importlib
import pkgutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any


@dataclass
class WpsResult:
    kind: str
    data: Any
    media_type: str = "application/json"
    filename: str | None = None


def _load_module(name: str) -> ModuleType:
    full_name = f"wps.{name}"
    if full_name in sys.modules:
        return importlib.reload(sys.modules[full_name])
    return importlib.import_module(full_name)


def discover_processes() -> dict[str, dict]:
    """Discover every valid algorithm source placed directly in the wps package."""
    package_path = Path(__file__).resolve().parent
    processes = {}
    for module_info in pkgutil.iter_modules([str(package_path)]):
        if module_info.name.startswith("_") or module_info.name == "registry":
            continue
        module = _load_module(module_info.name)
        metadata = getattr(module, "PROCESS", None)
        execute = getattr(module, "execute", None)
        if not isinstance(metadata, dict) or not callable(execute):
            continue
        process_id = str(metadata.get("id", "")).strip()
        if not process_id:
            continue
        public_metadata = {
            "id": process_id,
            "title_ko": metadata.get("title_ko", process_id),
            "title_en": metadata.get("title_en", process_id),
            "description": metadata.get("description", ""),
            "layer_types": list(metadata.get("layer_types", [])),
            "parameters": list(metadata.get("parameters", [])),
            "output": metadata.get("output", "json"),
            "source": f"wps/{module_info.name}.py",
        }
        processes[process_id] = {
            "metadata": public_metadata,
            "execute": execute,
        }
    return dict(sorted(processes.items()))


def execute_process(
    process_id: str, layer: dict, parameters: dict, context: dict
) -> WpsResult:
    process = discover_processes().get(process_id)
    if not process:
        raise ValueError(f"Unknown WPS process: {process_id}")
    allowed_types = process["metadata"]["layer_types"]
    if allowed_types and layer["type"] not in allowed_types:
        raise ValueError(
            f'Process "{process_id}" does not support layer type "{layer["type"]}"'
        )
    result = process["execute"](layer, parameters, context)
    if not isinstance(result, WpsResult):
        raise TypeError(f'WPS plugin "{process_id}" must return WpsResult')
    return result
