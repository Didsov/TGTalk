import unittest
from os import environ
from unittest.mock import patch

from src.config import requireSetting


class RequireSettingTests(unittest.TestCase):
    def test_returns_existing_setting(self) -> None:
        with patch.dict(environ, {"TEST_SETTING": "значение"}, clear=True):
            self.assertEqual(requireSetting("TEST_SETTING"), "значение")

    def test_rejects_missing_setting(self) -> None:
        with (
            patch.dict(environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "TEST_SETTING"),
        ):
            requireSetting("TEST_SETTING")
