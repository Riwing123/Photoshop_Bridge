from __future__ import annotations

import hashlib
import html
import json
import math
import re
from typing import Any


SCHEMA_VERSION = "ps-agent/v1"
MAX_VISUAL_PARTS = 16
MAX_GENERATED_ASSETS = 24
MAX_PATHS_PER_ASSET = 96
MAX_SVG_BYTES = 96 * 1024
MAX_COMMANDS_PER_OBJECT = 4096
ALLOWED_ROLES = {
    "shadow",
    "base_fill",
    "secondary_fill",
    "texture",
    "outline",
    "highlight",
    "accent",
}
ROLE_ORDER = {
    "shadow": 10,
    "base_fill": 20,
    "secondary_fill": 30,
    "texture": 40,
    "outline": 50,
    "highlight": 60,
    "accent": 70,
}
COMMAND_ARITY = {"M": 2, "L": 2, "C": 6, "Q": 4, "A": 7, "Z": 0}
RAW_PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE+\-.,\s]+$")
ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "error",
        "schema_version": SCHEMA_VERSION,
        "error": {"code": code, "message": message},
    }
    if details is not None:
        result["error"]["details"] = details
    return result


def _safe_id(value: Any, fallback: str) -> str:
    cleaned = ID_RE.sub("_", str(value or "")).strip("._:-")
    return (cleaned or fallback)[:64]


def _number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if abs(number) > 10_000_000:
        raise ValueError(f"{label} is outside the supported coordinate range")
    return number


def _fmt(value: float) -> str:
    rounded = round(float(value), 4)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def _color(value: Any, fallback: str = "none") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{3,8}", stripped) or re.fullmatch(r"[A-Za-z]+", stripped):
            return stripped
        raise ValueError(f"Unsupported SVG color: {value}")
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        rgb = [max(0, min(255, round(_number(value[i], f"color[{i}]")))) for i in range(3)]
        return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"
    if isinstance(value, dict):
        rgb = value.get("rgb")
        if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
            return _color(rgb, fallback)
        if all(key in value for key in ("r", "g", "b")):
            return _color([value["r"], value["g"], value["b"]], fallback)
    raise ValueError(f"Unsupported SVG color value: {value!r}")


def _view_box(value: Any, width: float, height: float) -> tuple[float, float, float, float]:
    if isinstance(value, str):
        pieces = re.split(r"[\s,]+", value.strip())
        if len(pieces) == 4:
            result = tuple(_number(float(piece), "view_box") for piece in pieces)
        else:
            raise ValueError("view_box string must contain four numbers")
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        result = tuple(_number(value[i], f"view_box[{i}]") for i in range(4))
    elif isinstance(value, dict):
        result = (
            _number(value.get("x", value.get("min_x", 0)), "view_box.x"),
            _number(value.get("y", value.get("min_y", 0)), "view_box.y"),
            _number(value.get("width", value.get("w", width)), "view_box.width"),
            _number(value.get("height", value.get("h", height)), "view_box.height"),
        )
    else:
        result = (0.0, 0.0, width, height)
    if result[2] <= 0 or result[3] <= 0:
        raise ValueError("view_box width and height must be positive")
    return result


def _command_values(command: Any, path: str) -> tuple[str, list[float]]:
    if isinstance(command, (list, tuple)) and command:
        op = str(command[0]).upper()
        values = list(command[1:])
    elif isinstance(command, dict):
        op = str(command.get("cmd") or command.get("op") or "").upper()
        if op in {"M", "L"}:
            values = [command.get("x"), command.get("y")]
        elif op == "C":
            values = [command.get("x1"), command.get("y1"), command.get("x2"), command.get("y2"), command.get("x"), command.get("y")]
        elif op == "Q":
            values = [command.get("x1"), command.get("y1"), command.get("x"), command.get("y")]
        elif op == "A":
            values = [
                command.get("rx"), command.get("ry"), command.get("rotation", 0),
                command.get("large_arc", command.get("largeArc", 0)), command.get("sweep", 0),
                command.get("x"), command.get("y"),
            ]
        else:
            values = []
    else:
        raise ValueError(f"{path} must be a command object or array")
    if op not in COMMAND_ARITY:
        raise ValueError(f"{path} uses unsupported command {op!r}")
    if len(values) != COMMAND_ARITY[op]:
        raise ValueError(f"{path} command {op} requires {COMMAND_ARITY[op]} values")
    parsed = [_number(value, f"{path}.{op}[{index}]") for index, value in enumerate(values)]
    if op == "A":
        parsed[3] = 1.0 if parsed[3] else 0.0
        parsed[4] = 1.0 if parsed[4] else 0.0
        if parsed[0] < 0 or parsed[1] < 0:
            raise ValueError(f"{path} arc radii must be non-negative")
    return op, parsed


def _path_data(path_spec: Any, path: str) -> tuple[str, int]:
    if isinstance(path_spec, str):
        raw = path_spec.strip()
        if not raw or not RAW_PATH_RE.fullmatch(raw):
            raise ValueError(f"{path} contains unsupported raw SVG path data")
        return raw, len(re.findall(r"[A-Za-z]", raw))
    if not isinstance(path_spec, dict):
        raise ValueError(f"{path} must be an object or raw path string")
    raw = path_spec.get("raw_d", path_spec.get("d", path_spec.get("path_data")))
    if isinstance(raw, str) and raw.strip():
        return _path_data(raw, path)
    commands = path_spec.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError(f"{path}.commands must be a non-empty array")
    pieces: list[str] = []
    for index, command in enumerate(commands):
        op, values = _command_values(command, f"{path}.commands[{index}]")
        pieces.append(op if not values else f"{op} {' '.join(_fmt(value) for value in values)}")
    return " ".join(pieces), len(commands)


def _gradient_definition(value: dict[str, Any], gradient_id: str) -> str:
    kind = str(value.get("type") or value.get("kind") or "linear").lower()
    stops = value.get("stops")
    if not isinstance(stops, list) or len(stops) < 2:
        raise ValueError("SVG gradient requires at least two stops")
    stop_tags = []
    for index, stop in enumerate(stops):
        if not isinstance(stop, dict):
            raise ValueError(f"gradient stop {index} must be an object")
        offset = max(0.0, min(1.0, _number(stop.get("offset", index / max(1, len(stops) - 1)), "gradient.offset")))
        color = _color(stop.get("color"), "#000000")
        opacity = max(0.0, min(1.0, _number(stop.get("opacity", 1), "gradient.opacity")))
        stop_tags.append(f'<stop offset="{_fmt(offset * 100)}%" stop-color="{html.escape(color, quote=True)}" stop-opacity="{_fmt(opacity)}"/>')
    if kind in {"radial", "radial_gradient"}:
        attrs = {
            "cx": value.get("cx", "50%"), "cy": value.get("cy", "50%"), "r": value.get("r", "50%"),
        }
        attr_text = " ".join(f'{key}="{html.escape(str(val), quote=True)}"' for key, val in attrs.items())
        return f'<radialGradient id="{gradient_id}" {attr_text}>{"".join(stop_tags)}</radialGradient>'
    attrs = {
        "x1": value.get("x1", "0%"), "y1": value.get("y1", "0%"),
        "x2": value.get("x2", "100%"), "y2": value.get("y2", "0%"),
    }
    attr_text = " ".join(f'{key}="{html.escape(str(val), quote=True)}"' for key, val in attrs.items())
    return f'<linearGradient id="{gradient_id}" {attr_text}>{"".join(stop_tags)}</linearGradient>'


def _paint_attributes(paint: dict[str, Any], namespace: str) -> tuple[str, str]:
    definitions: list[str] = []
    fill_value = paint.get("fill", "none")
    if isinstance(fill_value, dict) and str(fill_value.get("type") or fill_value.get("kind") or "").lower() in {
        "linear", "linear_gradient", "radial", "radial_gradient"
    }:
        gradient_id = f"{namespace}_fill"
        definitions.append(_gradient_definition(fill_value, gradient_id))
        fill = f"url(#{gradient_id})"
    else:
        fill = _color(fill_value, "none")
    stroke_value = paint.get("stroke")
    stroke = _color(stroke_value, "none") if stroke_value is not None else "none"
    stroke_width = max(0.0, _number(paint.get("stroke_width", 0), "paint.stroke_width"))
    opacity = max(0.0, min(1.0, _number(paint.get("opacity", 1), "paint.opacity")))
    fill_rule = str(paint.get("fill_rule", "nonzero")).lower()
    if fill_rule not in {"nonzero", "evenodd"}:
        raise ValueError("paint.fill_rule must be nonzero or evenodd")
    linecap = str(paint.get("linecap", "round")).lower()
    linejoin = str(paint.get("linejoin", "round")).lower()
    if linecap not in {"butt", "round", "square"}:
        raise ValueError("paint.linecap must be butt, round, or square")
    if linejoin not in {"miter", "round", "bevel"}:
        raise ValueError("paint.linejoin must be miter, round, or bevel")
    attrs = (
        f'fill="{html.escape(fill, quote=True)}" stroke="{html.escape(stroke, quote=True)}" '
        f'stroke-width="{_fmt(stroke_width)}" stroke-linecap="{linecap}" stroke-linejoin="{linejoin}" '
        f'fill-rule="{fill_rule}" opacity="{_fmt(opacity)}"'
    )
    return attrs, "".join(definitions)


def _render_svg(paths: list[dict[str, Any]], paint: dict[str, Any], view_box: tuple[float, float, float, float], namespace: str) -> tuple[str, int]:
    attrs, definitions = _paint_attributes(paint, namespace)
    tags = []
    command_count = 0
    for index, path_spec in enumerate(paths):
        d, count = _path_data(path_spec, f"paths[{index}]")
        command_count += count
        tags.append(f'<path d="{html.escape(d, quote=True)}" {attrs}/>')
    x, y, width, height = view_box
    defs = f"<defs>{definitions}</defs>" if definitions else ""
    guard_size = max(0.5, min(width, height) * 0.002)
    guard = (
        f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(guard_size)}" height="{_fmt(guard_size)}" fill="#000000" opacity="0.01"/>'
        f'<rect x="{_fmt(x + width - guard_size)}" y="{_fmt(y + height - guard_size)}" width="{_fmt(guard_size)}" height="{_fmt(guard_size)}" fill="#000000" opacity="0.01"/>'
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width)}" height="{_fmt(height)}" '
        f'viewBox="{_fmt(x)} {_fmt(y)} {_fmt(width)} {_fmt(height)}" preserveAspectRatio="none">'
        f'{defs}{guard}{"".join(tags)}</svg>'
    )
    return svg, command_count


def _split_part_paths(paths: list[dict[str, Any]], paint: dict[str, Any], view_box: tuple[float, float, float, float], namespace: str) -> list[tuple[str, list[dict[str, Any]], int]]:
    pending = [paths[index:index + MAX_PATHS_PER_ASSET] for index in range(0, len(paths), MAX_PATHS_PER_ASSET)]
    rendered: list[tuple[str, list[dict[str, Any]], int]] = []
    while pending:
        chunk = pending.pop(0)
        svg, command_count = _render_svg(chunk, paint, view_box, f"{namespace}_{len(rendered) + len(pending)}")
        if len(svg.encode("utf-8")) > MAX_SVG_BYTES and len(chunk) > 1:
            middle = max(1, len(chunk) // 2)
            pending.insert(0, chunk[middle:])
            pending.insert(0, chunk[:middle])
            continue
        if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
            raise ValueError(f"single SVG path exceeds {MAX_SVG_BYTES} bytes")
        rendered.append((svg, chunk, command_count))
    return rendered


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _structured_path_samples(path_spec: Any, sample_count: int = 16) -> list[list[tuple[float, float]]] | None:
    if not isinstance(path_spec, dict) or not isinstance(path_spec.get("commands"), list):
        return None
    subpaths: list[list[tuple[float, float]]] = []
    active: list[tuple[float, float]] = []
    current: tuple[float, float] | None = None
    start: tuple[float, float] | None = None
    for index, command in enumerate(path_spec["commands"]):
        op, values = _command_values(command, f"audit.commands[{index}]")
        if op == "M":
            if len(active) > 1:
                subpaths.append(active)
            current = (values[0], values[1])
            start = current
            active = [current]
        elif current is None:
            raise ValueError("structured path must begin with M")
        elif op == "L":
            current = (values[0], values[1])
            active.append(current)
        elif op == "C":
            p0 = current
            p1 = (values[0], values[1])
            p2 = (values[2], values[3])
            p3 = (values[4], values[5])
            for step in range(1, sample_count + 1):
                t = step / sample_count
                mt = 1.0 - t
                active.append((
                    mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
                    mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1],
                ))
            current = p3
        elif op == "Q":
            p0 = current
            p1 = (values[0], values[1])
            p2 = (values[2], values[3])
            for step in range(1, sample_count + 1):
                t = step / sample_count
                mt = 1.0 - t
                active.append((
                    mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                    mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1],
                ))
            current = p2
        elif op == "A":
            endpoint = (values[5], values[6])
            for step in range(1, sample_count + 1):
                t = step / sample_count
                active.append((_lerp(current[0], endpoint[0], t), _lerp(current[1], endpoint[1], t)))
            current = endpoint
        elif op == "Z" and start is not None:
            if active[-1] != start:
                active.append(start)
            current = start
    if len(active) > 1:
        subpaths.append(active)
    return subpaths


def _proper_segment_intersection(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]) -> bool:
    def cross(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    ab_c = cross(a, b, c)
    ab_d = cross(a, b, d)
    cd_a = cross(c, d, a)
    cd_b = cross(c, d, b)
    epsilon = 1e-7
    return ((ab_c > epsilon and ab_d < -epsilon) or (ab_c < -epsilon and ab_d > epsilon)) and ((cd_a > epsilon and cd_b < -epsilon) or (cd_a < -epsilon and cd_b > epsilon))


def _subpath_has_self_intersection(points: list[tuple[float, float]]) -> bool:
    segment_count = len(points) - 1
    if segment_count < 4:
        return False
    closed = points[0] == points[-1]
    for first in range(segment_count):
        for second in range(first + 1, segment_count):
            if abs(first - second) <= 1:
                continue
            if closed and first == 0 and second == segment_count - 1:
                continue
            if _proper_segment_intersection(points[first], points[first + 1], points[second], points[second + 1]):
                return True
    return False


def _audit_svg_paths(paths: list[Any], view_box: tuple[float, float, float, float], part_id: str) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    min_x, min_y, width, height = view_box
    max_x = min_x + width
    max_y = min_y + height
    bleed_x = width * 0.1
    bleed_y = height * 0.1
    for path_index, path_spec in enumerate(paths):
        sampled = _structured_path_samples(path_spec)
        if sampled is None:
            warnings.append({"code": "svg_raw_path_not_sampled", "part_id": part_id, "path_index": path_index})
            continue
        if any(_subpath_has_self_intersection(subpath) for subpath in sampled):
            warnings.append({"code": "svg_self_intersection", "part_id": part_id, "path_index": path_index})
        points = [point for subpath in sampled for point in subpath]
        if points and any(point[0] < min_x - bleed_x or point[0] > max_x + bleed_x or point[1] < min_y - bleed_y or point[1] > max_y + bleed_y for point in points):
            warnings.append({"code": "svg_path_outside_view_box", "part_id": part_id, "path_index": path_index})
    return warnings

def compile_svg_object(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("vector_object") or payload.get("object") or payload
    if not isinstance(source, dict):
        return _error("invalid_svg_object", "vector_object must be an object")
    try:
        object_id = _safe_id(source.get("object_id") or source.get("node_id"), "vector_object")
        name = str(source.get("name") or source.get("role") or object_id)
        bbox = source.get("bbox") if isinstance(source.get("bbox"), dict) else {}
        x = _number(bbox.get("x", source.get("x", 0)), "bbox.x")
        y = _number(bbox.get("y", source.get("y", 0)), "bbox.y")
        width = _number(bbox.get("width", bbox.get("w", source.get("width", 0))), "bbox.width")
        height = _number(bbox.get("height", bbox.get("h", source.get("height", 0))), "bbox.height")
        if width <= 0 or height <= 0:
            raise ValueError("bbox width and height must be positive")
        view_box = _view_box(source.get("view_box", source.get("viewBox")), width, height)
        parts = source.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError("parts must be a non-empty array")
        if len(parts) > MAX_VISUAL_PARTS:
            raise ValueError(f"parts must contain at most {MAX_VISUAL_PARTS} visual layers")

        compiled_parts: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        total_commands = 0
        seen_part_ids: set[str] = set()
        sorted_parts = sorted(enumerate(parts), key=lambda item: (float(item[1].get("z_order", ROLE_ORDER.get(str(item[1].get("role") or "base_fill"), 20))) if isinstance(item[1], dict) else 0, item[0]))
        for original_index, part in sorted_parts:
            if not isinstance(part, dict):
                raise ValueError(f"parts[{original_index}] must be an object")
            role = str(part.get("role") or "base_fill").lower()
            if role not in ALLOWED_ROLES:
                raise ValueError(f"parts[{original_index}].role is unsupported: {role}")
            part_id = _safe_id(part.get("part_id"), f"{role}_{original_index + 1}")
            if part_id in seen_part_ids:
                raise ValueError(f"duplicate part_id: {part_id}")
            seen_part_ids.add(part_id)
            paths = part.get("paths")
            if not isinstance(paths, list) or not paths:
                raise ValueError(f"parts[{original_index}].paths must be a non-empty array")
            warnings.extend(_audit_svg_paths(paths, view_box, part_id))
            paint = part.get("paint") if isinstance(part.get("paint"), dict) else {}
            if not paint:
                paint = {key: part[key] for key in ("fill", "stroke", "stroke_width", "opacity", "fill_rule", "linecap", "linejoin") if key in part}
            shards = _split_part_paths(paths, paint, view_box, f"{object_id}_{part_id}")
            if len(shards) > 1:
                warnings.append({"code": "svg_part_sharded", "part_id": part_id, "shard_count": len(shards)})
            for shard_index, (svg, shard_paths, command_count) in enumerate(shards, start=1):
                total_commands += command_count
                shard_id = part_id if len(shards) == 1 else f"{part_id}__{shard_index:02d}"
                compiled_parts.append({
                    "part_id": shard_id,
                    "source_part_id": part_id,
                    "role": role,
                    "z_order": part.get("z_order", ROLE_ORDER[role]),
                    "name": str(part.get("name") or f"{name} - {shard_id}"),
                    "svg": svg,
                    "svg_hash": hashlib.sha256(svg.encode("utf-8")).hexdigest(),
                    "svg_bytes": len(svg.encode("utf-8")),
                    "path_count": len(shard_paths),
                    "command_count": command_count,
                    "opacity": part.get("layer_opacity", 100),
                    "blend_mode": part.get("blend_mode", "normal"),
                    "effects": part.get("effects") if isinstance(part.get("effects"), list) else [],
                })
        if len(compiled_parts) > MAX_GENERATED_ASSETS:
            raise ValueError(f"compiled object exceeds {MAX_GENERATED_ASSETS} SVG assets")
        if total_commands > MAX_COMMANDS_PER_OBJECT:
            raise ValueError(f"compiled object exceeds {MAX_COMMANDS_PER_OBJECT} SVG commands")

        step_prefix = _safe_id(source.get("step_id") or object_id, "vector_object")
        steps: list[dict[str, Any]] = []
        layer_refs: list[str] = []
        for index, part in enumerate(compiled_parts, start=1):
            step_id = _safe_id(f"{step_prefix}_{part['part_id']}", f"{step_prefix}_part_{index}")
            layer_refs.append(f"$steps.{step_id}.layer_id")
            steps.append({
                "step_id": step_id,
                "atom_id": "shape.svg_asset_place",
                "params": {
                    "name": part["name"],
                    "svg": part["svg"],
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                    "opacity": part["opacity"],
                    "blend_mode": part["blend_mode"],
                    "object_id": object_id,
                    "part_id": part["part_id"],
                    "style_role": part["role"],
                    "asset_hash": part["svg_hash"],
                },
            })
            for effect_index, effect in enumerate(part["effects"], start=1):
                if not isinstance(effect, dict) or not str(effect.get("atom_id") or "").startswith("layer.effect_"):
                    warnings.append({"code": "unsupported_svg_part_effect", "part_id": part["part_id"], "effect_index": effect_index})
                    continue
                effect_params = dict(effect.get("params") or {})
                effect_params.update({"object_id": object_id, "part_id": part["part_id"]})
                steps.append({
                    "step_id": _safe_id(f"{step_id}_effect_{effect_index}", "svg_effect"),
                    "atom_id": str(effect["atom_id"]),
                    "target": f"$steps.{step_id}.layer_id",
                    "params": effect_params,
                })

        group_step_id = _safe_id(f"{step_prefix}_group", "svg_group")
        steps.append({
            "step_id": group_step_id,
            "atom_id": "layer.group",
            "params": {"name": name, "layer_ids": layer_refs, "object_id": object_id},
        })
        rotation = source.get("rotation")
        if rotation is not None and abs(_number(rotation, "rotation")) > 1e-9:
            steps.append({
                "step_id": _safe_id(f"{step_prefix}_transform", "svg_transform"),
                "atom_id": "layer.transform",
                "target": f"$steps.{group_step_id}.group_id",
                "params": {"rotation": float(rotation), "object_id": object_id},
            })

        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "object_id": object_id,
            "operation_recipe_fragment": {"steps": steps},
            "object_manifest": {
                "object_id": object_id,
                "name": name,
                "bbox": {"x": x, "y": y, "width": width, "height": height},
                "view_box": list(view_box),
                "visual_part_count": len(parts),
                "generated_asset_count": len(compiled_parts),
                "command_count": total_commands,
                "group_step_id": group_step_id,
                "parts": [{key: part[key] for key in ("part_id", "source_part_id", "role", "svg_hash", "svg_bytes", "path_count", "command_count")} for part in compiled_parts],
            },
            "warnings": warnings,
        }
    except (TypeError, ValueError) as exc:
        return _error("invalid_svg_object", str(exc))

