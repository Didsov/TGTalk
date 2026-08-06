import unittest

from src.integrations.telegram.stub_bot import transform_message


class TransformMessageTests(unittest.TestCase):
    def test_doubles_integer(self) -> None:
        self.assertEqual(transform_message("123"), "246")

    def test_doubles_negative_decimal(self) -> None:
        self.assertEqual(transform_message("-2.5"), "-5")

    def test_ignores_surrounding_whitespace_for_number(self) -> None:
        self.assertEqual(transform_message(" 10 "), "20")

    def test_reverses_text(self) -> None:
        self.assertEqual(transform_message("Telegram"), "margeleT")

    def test_reverses_non_finite_decimal_literal(self) -> None:
        self.assertEqual(transform_message("NaN"), "NaN")


if __name__ == "__main__":
    unittest.main()

