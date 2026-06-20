from __future__ import annotations

import unittest

from backend.ps_backend.svg_geometry import compile_svg_object


def curve(offset: int = 0) -> dict:
    return {
        "commands": [
            ["M", 10 + offset, 80],
            ["C", 10 + offset, 30, 90 + offset, 30, 90 + offset, 80],
            ["Z"],
        ]
    }


class SvgGeometryTests(unittest.TestCase):
    def vector_object(self) -> dict:
        return {
            "object_id": "sticker",
            "bbox": {"x": 20, "y": 30, "width": 240, "height": 180},
            "view_box": [0, 0, 120, 90],
            "parts": [
                {"part_id": "base", "role": "base_fill", "paths": [curve()], "paint": {"fill": "#ff335f"}},
                {"part_id": "shine", "role": "highlight", "paths": [curve(5)], "paint": {"fill": "none", "stroke": "#ffffff", "stroke_width": 3}},
            ],
        }

    def test_compile_is_deterministic_and_grouped(self) -> None:
        first = compile_svg_object(self.vector_object())
        second = compile_svg_object(self.vector_object())
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["object_manifest"]["parts"], second["object_manifest"]["parts"])
        steps = first["operation_recipe_fragment"]["steps"]
        self.assertEqual([step["atom_id"] for step in steps], ["shape.svg_asset_place", "shape.svg_asset_place", "layer.group"])
        self.assertEqual(steps[0]["params"]["object_id"], "sticker")

    def test_large_visual_layer_is_sharded(self) -> None:
        value = self.vector_object()
        value["parts"] = [{"part_id": "texture", "role": "texture", "paths": [curve(index) for index in range(97)], "paint": {"fill": "#00aa66"}}]
        result = compile_svg_object(value)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["object_manifest"]["generated_asset_count"], 2)
        self.assertTrue(any(item["code"] == "svg_part_sharded" for item in result["warnings"]))

    def test_reports_structured_self_intersection(self) -> None:
        value = self.vector_object()
        value["parts"] = [{"part_id": "knot", "role": "base_fill", "paths": [{"commands": [["M", 10, 10], ["L", 90, 90], ["L", 90, 10], ["L", 10, 90], ["Z"]]}], "paint": {"fill": "#ff335f"}}]
        result = compile_svg_object(value)
        self.assertTrue(any(item["code"] == "svg_self_intersection" for item in result["warnings"]))

    def test_rejects_unsupported_raw_path_content(self) -> None:
        value = self.vector_object()
        value["parts"][0]["paths"] = [{"raw_d": "<script>alert(1)</script>"}]
        result = compile_svg_object(value)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_svg_object")


if __name__ == "__main__":
    unittest.main()