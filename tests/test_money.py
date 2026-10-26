from decimal import Decimal
import unittest

from eluno_pos.money import MoneyConversionError, decimal_string_from_paise, paise_from_decimal


class MoneyTests(unittest.TestCase):
    def test_external_decimal_converts_exactly_to_paise(self) -> None:
        self.assertEqual(1907215, paise_from_decimal("19072.15"))
        self.assertEqual(-1, paise_from_decimal(Decimal("-0.01")))

    def test_paise_serializes_to_two_decimal_places(self) -> None:
        self.assertEqual("19072.15", decimal_string_from_paise(1907215))
        self.assertEqual("-0.01", decimal_string_from_paise(-1))

    def test_sub_paisa_values_are_rejected(self) -> None:
        with self.assertRaises(MoneyConversionError):
            paise_from_decimal("1.001")


if __name__ == "__main__":
    unittest.main()

