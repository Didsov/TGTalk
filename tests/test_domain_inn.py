import unittest

from src.domain import is_valid_inn


class InnValidationTests(unittest.TestCase):
    def test_accepts_valid_organization_and_entrepreneur_inn(self) -> None:
        self.assertTrue(is_valid_inn("7707083893"))
        self.assertTrue(is_valid_inn("500100732259"))

    def test_rejects_invalid_values(self) -> None:
        for value in (
            "",
            "123",
            "7707083894",
            "500100732250",
            "abcdefghij",
            None,
        ):
            with self.subTest(value=value):
                self.assertFalse(is_valid_inn(value))


if __name__ == "__main__":
    unittest.main()
