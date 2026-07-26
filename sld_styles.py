from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _css_parameters(root: ElementTree.Element, symbolizer: str) -> dict[str, str]:
    result = {}
    for element in root.iter():
        if _local_name(element.tag) != symbolizer:
            continue
        for child in element.iter():
            if _local_name(child.tag) in ("CssParameter", "SvgParameter"):
                name = child.attrib.get("name", "").strip()
                if name and child.text:
                    result[name] = child.text.strip()
        break
    return result


def read_sld(path: Path) -> dict:
    root = ElementTree.parse(path).getroot()
    parameters = (
        _css_parameters(root, "PolygonSymbolizer")
        or _css_parameters(root, "LineSymbolizer")
        or _css_parameters(root, "PointSymbolizer")
    )
    style = {}
    for source, target in (
        ("stroke", "stroke"),
        ("fill", "fill"),
        ("fill-opacity", "fill_opacity"),
        ("stroke-width", "stroke_width"),
    ):
        if source in parameters:
            style[target] = parameters[source]
    for element in root.iter():
        if _local_name(element.tag) == "Size" and element.text:
            try:
                style["point_size"] = float(element.text.strip())
            except ValueError:
                pass
            break
    color_map = []
    for element in root.iter():
        if _local_name(element.tag) != "ColorMapEntry":
            continue
        try:
            color_map.append({
                "quantity": float(element.attrib["quantity"]),
                "color": element.attrib["color"],
                "opacity": float(element.attrib.get("opacity", "1")),
                "label": element.attrib.get("label", ""),
            })
        except (KeyError, ValueError):
            continue
    color_map.sort(key=lambda entry: entry["quantity"])
    return {"style": style, "color_map": color_map}


def apply_sld(layer: dict, base_dir: Path) -> dict:
    if not layer.get("sld_path"):
        return layer
    parsed = read_sld(base_dir / layer["sld_path"])
    styled = dict(layer)
    styled["style"] = {**layer.get("style", {}), **parsed["style"]}
    if parsed["color_map"]:
        styled["_sld_colormap"] = parsed["color_map"]
    return styled
