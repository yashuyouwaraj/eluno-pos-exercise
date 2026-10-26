import builtins
import importlib.util
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch


MOCK_PATH = Path(__file__).resolve().parents[1] / "starter-kit" / "mock-apis" / "mock_server.py"
SPEC = importlib.util.spec_from_file_location("eluno_test_mock_server", str(MOCK_PATH))
MOCK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOCK)


class MockPrivacyTests(unittest.TestCase):
    def test_seed_files_are_opened_as_utf8(self):
        with patch("builtins.open", wraps=builtins.open) as open_mock:
            MOCK.L("offers.json")
        seed_call = next(call for call in open_mock.call_args_list if str(call.args[0]).endswith("offers.json"))
        self.assertEqual("utf-8", seed_call.kwargs.get("encoding"))

    def test_request_logging_omits_query_string(self):
        handler = object.__new__(MOCK.H)
        handler.command = "GET"
        handler.path = "/customers/search.json?query=phone%3A%2B919876543210"
        output = StringIO()
        with redirect_stderr(output):
            handler.log_message("%s", "unused")
        self.assertIn("/customers/search.json", output.getvalue())
        self.assertNotIn("919876543210", output.getvalue())
        self.assertNotIn("query=", output.getvalue())


if __name__ == "__main__":
    unittest.main()
