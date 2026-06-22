import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import validate_native_selection_mask
from ps_backend.alpha_masks import materialize_full_document_alpha_mask
from ps_backend.selection_atoms import validate_selection_recipe


class NativeSelectionMaskTests(unittest.TestCase):
    def test_materialize_full_document_alpha_mask_writes_gray_and_png_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "input.gray"
            raw_path.write_bytes(bytes([0, 64, 128, 255]))
            out_dir = root / "out"
            out_dir.mkdir()
            result = materialize_full_document_alpha_mask(
                raw_path,
                out_dir,
                "job-test",
                {"width": 2, "height": 2},
                threshold=0.5,
                feather=0,
                invert=True,
            )
            self.assertTrue(result["alpha_path"].is_file())
            self.assertTrue(result["luma_path"].is_file())
            self.assertTrue(result["raw_path"].is_file())
            self.assertEqual(result["raw_path"].read_bytes(), bytes([255, 191, 127, 0]))
            self.assertEqual(result["relative"]["raw"], "job-test/alpha_mask.gray")
            self.assertIn("mask_inverted", result["warnings"])

    def test_materialize_validation_does_not_require_selector_inputs(self):
        strict = validate_native_selection_mask({"action": "color_range"})
        materialize = validate_native_selection_mask({"action": "color_range"}, require_selector_input=False)
        self.assertFalse(strict["valid"])
        self.assertTrue(materialize["valid"])


    def test_validate_selection_recipe_rejects_native_generator_atoms(self):
        recipe = {
            "schema_version": "ps-agent/v1",
            "recipe_id": "sel-native-generator",
            "goal": "Test native generator rejection.",
            "candidates": [
                {
                    "candidate_id": "sky_native",
                    "atom_id": "selection.select_sky",
                    "role": "base",
                    "params": {},
                }
            ],
            "merge_plan": {
                "mode": "soft_alpha",
                "items": [{"candidate_id": "sky_native", "operation": "replace"}],
            },
            "review": {"overlay": True, "regions": ["region_bounds"]},
        }
        result = validate_selection_recipe({"selection_recipe": recipe})
        self.assertFalse(result["valid"])
        self.assertTrue(any("native alpha generator" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
