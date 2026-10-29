import sys
import io
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import start_local


class Process:
    def __init__(self): self.returncode=None; self.terminated=False
    def poll(self): return self.returncode
    def terminate(self): self.terminated=True; self.returncode=0
    def wait(self, timeout=None): return 0
    def kill(self): self.returncode=-9


class StartupTests(unittest.TestCase):
    def test_default_opens_browser_after_health_and_does_not_start_cli(self):
        mock, app = Process(), Process()
        events=[]
        def healthy(*_args):
            events.append("health")
        def opened(url):
            events.append(("browser",url))
            return True
        with patch.object(start_local, "assert_port_available"), \
             patch.object(start_local, "start_process", side_effect=[mock, app]) as start, \
             patch.object(start_local, "wait_for_health", side_effect=healthy), \
             patch.object(start_local.webbrowser, "open", side_effect=opened), \
             patch.object(start_local.time, "sleep", side_effect=KeyboardInterrupt), \
             patch.object(start_local.subprocess, "call") as call, \
             patch.object(sys, "argv", ["start_local.py"]):
            self.assertEqual(0, start_local.main())
        self.assertEqual(["health","health",("browser","http://127.0.0.1:8790/")],events)
        call.assert_not_called()
        app_env=start.call_args_list[1].kwargs["env"]
        self.assertEqual("1",app_env["ELUNO_LOCAL_MOCK_MODE"])
        self.assertEqual(str(start_local.MOCK_PORT),app_env["ELUNO_LOCAL_MOCK_PORT"])
        self.assertTrue(mock.terminated); self.assertTrue(app.terminated)

    def test_cli_is_started_only_when_explicitly_requested(self):
        mock, app = Process(), Process()
        with patch.object(start_local, "assert_port_available"), \
             patch.object(start_local, "start_process", side_effect=[mock, app]), \
             patch.object(start_local, "wait_for_health"), \
             patch.object(start_local.webbrowser, "open", return_value=True) as browser, \
             patch.object(start_local.subprocess, "call", return_value=0) as call, \
             patch.object(sys, "argv", ["start_local.py", "--cli"]):
            self.assertEqual(0, start_local.main())
        browser.assert_called_once_with("http://127.0.0.1:8790/")
        self.assertEqual([sys.executable, "-m", "eluno_pos.cli"], call.call_args.args[0][:3])
        self.assertTrue(mock.terminated); self.assertTrue(app.terminated)

    def test_browser_failure_prints_manual_url(self):
        output=io.StringIO()
        with patch.object(start_local.webbrowser, "open", return_value=False), redirect_stdout(output):
            start_local.open_browser("http://127.0.0.1:8790/")
        self.assertIn("Open http://127.0.0.1:8790/ manually",output.getvalue())

    def test_browser_exception_prints_manual_url(self):
        output=io.StringIO()
        with patch.object(start_local.webbrowser, "open", side_effect=start_local.webbrowser.Error("blocked")), redirect_stdout(output):
            start_local.open_browser("http://127.0.0.1:8790/")
        self.assertIn("Open http://127.0.0.1:8790/ manually",output.getvalue())

    def test_no_cli_remains_an_accepted_browser_only_option(self):
        mock, app = Process(), Process()
        with patch.object(start_local, "assert_port_available"), \
             patch.object(start_local, "start_process", side_effect=[mock, app]), \
             patch.object(start_local, "wait_for_health"), \
             patch.object(start_local.webbrowser, "open", return_value=True), \
             patch.object(start_local.time, "sleep", side_effect=KeyboardInterrupt), \
             patch.object(start_local.subprocess, "call") as call, \
             patch.object(sys, "argv", ["start_local.py", "--no-cli"]):
            self.assertEqual(0, start_local.main())
        call.assert_not_called()
        self.assertTrue(mock.terminated); self.assertTrue(app.terminated)

    def test_cli_and_no_cli_flags_conflict(self):
        with patch.object(sys, "argv", ["start_local.py", "--cli", "--no-cli"]):
            with self.assertRaises(SystemExit):
                start_local.main()

    def test_partial_startup_stops_mock_and_returns_failure(self):
        mock = Process()
        with patch.object(start_local, "assert_port_available"), \
             patch.object(start_local, "start_process", return_value=mock), \
             patch.object(start_local, "wait_for_health", side_effect=RuntimeError("mock unavailable")), \
             patch.object(sys, "argv", ["start_local.py"]):
            self.assertEqual(1, start_local.main())
        self.assertTrue(mock.terminated)
