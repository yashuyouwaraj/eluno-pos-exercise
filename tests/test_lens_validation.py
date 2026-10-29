from dataclasses import replace
import json
from pathlib import Path
import unittest

from eluno_pos.adapters.shopify import ShopifyAdminClient
from eluno_pos.models import EyePrescription, LensConfiguration, Prescription
from eluno_pos.services import LensValidator


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "starter-kit" / "data"
PRODUCTS = json.loads((DATA / "products.json").read_text(encoding="utf-8"))
PRESCRIPTIONS = json.loads((DATA / "prescriptions.json").read_text(encoding="utf-8"))


class FixtureHttp:
    def get(self, path, query=None):
        self.path = path
        self.query = query
        return {"products": PRODUCTS}


class LensValidationTests(unittest.TestCase):
    def setUp(self):
        self.options = ShopifyAdminClient(http=FixtureHttp()).list_lens_options()
        self.validator = LensValidator()
        self.progressive_rx = Prescription.from_data(PRESCRIPTIONS[0])

    def test_valid_configuration_uses_catalog_matrix_metadata(self):
        result = self.validator.validate(
            LensConfiguration.from_data({"lens_type": "progressive", "index": "1.60", "coating": "blue_cut"}),
            self.progressive_rx,
            self.options,
        )
        self.assertTrue(result.is_valid)
        self.assertEqual("progressive", result.option.lens_type)

    def test_unknown_lens_combination_is_actionable(self):
        result = self.validator.validate(
            LensConfiguration.from_data({"lens_type": "single_vision", "index": "1.50", "coating": "polarized"}),
            self.progressive_rx,
            self.options,
        )
        self.assertFalse(result.is_valid)
        self.assertIn("not available", result.errors[0])

    def test_sphere_and_cylinder_limits_are_checked_per_eye(self):
        rx = replace(
            self.progressive_rx,
            od=EyePrescription.from_data({"sph": "-9.00", "cyl": "-5.00", "axis": 90, "add": "2.00"}),
        )
        result = self.validator.validate(
            LensConfiguration.from_data({"lens_type": "progressive", "index": "1.60", "coating": "standard"}),
            rx,
            self.options,
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any("sphere" in error for error in result.errors))
        self.assertTrue(any("cylinder" in error for error in result.errors))

    def test_progressive_requires_add_without_changing_prescription(self):
        no_add_rx = Prescription.from_data(PRESCRIPTIONS[1])
        result = self.validator.validate(
            LensConfiguration.from_data({"lens_type": "progressive", "index": "1.60", "coating": "standard"}),
            no_add_rx,
            self.options,
        )
        self.assertFalse(result.is_valid)
        self.assertTrue(any("ADD is required" in error for error in result.errors))
        self.assertIsNone(no_add_rx.od.add)

    def test_axis_values_on_five_degree_grid_are_valid_for_both_eyes(self):
        rx = replace(
            self.progressive_rx,
            od=EyePrescription.from_data({"sph": "-4.00", "cyl": "-0.50", "axis": 0, "add": "2.00"}),
            os=EyePrescription.from_data({"sph": "-3.75", "cyl": "-0.50", "axis": 180, "add": "2.00"}),
        )
        result = self.validator.validate(
            LensConfiguration.from_data({"lens_type": "progressive", "index": "1.60", "coating": "standard"}),
            rx,
            self.options,
        )
        self.assertTrue(result.is_valid, result.errors)

    def test_off_grid_axis_is_allowed_for_optometrist_review_for_each_eye(self):
        configuration = LensConfiguration.from_data(
            {"lens_type": "progressive", "index": "1.60", "coating": "standard"}
        )
        for field, eye_name in (("od", "Right eye"), ("os", "Left eye")):
            with self.subTest(eye=eye_name):
                rx = replace(
                    self.progressive_rx,
                    **{field: EyePrescription.from_data(
                        {"sph": "-4.00", "cyl": "-0.50", "axis": 91, "add": "2.00"}
                    )},
                )
                result = self.validator.validate(configuration, rx, self.options)
                self.assertTrue(result.is_valid, result.errors)

    def test_axis_range_missing_and_non_applicable_axis_rules(self):
        configuration = LensConfiguration.from_data(
            {"lens_type": "progressive", "index": "1.60", "coating": "standard"}
        )
        for axis in (-5, 181):
            with self.subTest(axis=axis):
                rx = replace(
                    self.progressive_rx,
                    od=EyePrescription.from_data(
                        {"sph": "-4.00", "cyl": "-0.50", "axis": axis, "add": "2.00"}
                    ),
                )
                result = self.validator.validate(configuration, rx, self.options)
                self.assertTrue(any("Right eye axis" in error and "supported range" in error for error in result.errors))

        missing_required = replace(
            self.progressive_rx,
            os=EyePrescription.from_data({"sph": "-3.75", "cyl": "-0.50", "axis": None, "add": "2.00"}),
        )
        result = self.validator.validate(configuration, missing_required, self.options)
        self.assertTrue(any("Left eye axis is required" in error for error in result.errors))

        not_applicable = replace(
            self.progressive_rx,
            od=EyePrescription.from_data({"sph": "-4.00", "cyl": "0.00", "axis": None, "add": "2.00"}),
        )
        self.assertTrue(self.validator.validate(configuration, not_applicable, self.options).is_valid)

    def test_fractional_or_malformed_axis_is_not_coerced(self):
        for axis in (90.5, "90.5", "not-an-axis", True):
            with self.subTest(axis=axis):
                with self.assertRaisesRegex(ValueError, "Right eye axis must be an integer"):
                    Prescription.from_data({
                        **PRESCRIPTIONS[0],
                        "od": {**PRESCRIPTIONS[0]["od"], "axis": axis},
                    })

    def test_bifocal_add_is_required_and_uses_catalog_add_bounds(self):
        missing_add = Prescription.from_data(PRESCRIPTIONS[1])
        missing_result = self.validator.validate(
            LensConfiguration.from_data({"lens_type": "bifocal", "index": "1.60", "coating": "standard"}),
            missing_add,
            self.options,
        )
        self.assertFalse(missing_result.is_valid)
        self.assertTrue(any("ADD is required" in error for error in missing_result.errors))

        out_of_range = replace(
            self.progressive_rx,
            od=EyePrescription.from_data({"sph": "-4.00", "cyl": "0.00", "axis": None, "add": "3.25"}),
        )
        range_result = self.validator.validate(
            LensConfiguration.from_data({"lens_type": "progressive", "index": "1.60", "coating": "standard"}),
            out_of_range,
            self.options,
        )
        self.assertFalse(range_result.is_valid)
        self.assertTrue(any("ADD" in error and "maximum" in error for error in range_result.errors))


if __name__ == "__main__":
    unittest.main()
