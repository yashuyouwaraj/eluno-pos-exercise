import sys
import unittest
from pathlib import Path
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
    def test_default_starts_cli_then_stops_services(self):
        mock, app = Process(), Process()
        with patch.object(start_local, "assert_port_available"), \
             patch.object(start_local, "start_process", side_effect=[mock, app]) as start, \
             patch.object(start_local, "wait_for_health"), \
             patch.object(start_local.subprocess, "call", return_value=0) as call, \
             patch.object(sys, "argv", ["start_local.py"]):
            self.assertEqual(0, start_local.main())
        self.assertEqual([sys.executable, "-m", "eluno_pos.cli"], call.call_args.args[0][:3])
        app_env=start.call_args_list[1].kwargs["env"]
        self.assertEqual("1",app_env["ELUNO_LOCAL_MOCK_MODE"])
        self.assertEqual(str(start_local.MOCK_PORT),app_env["ELUNO_LOCAL_MOCK_PORT"])
        self.assertTrue(mock.terminated); self.assertTrue(app.terminated)

    def test_partial_startup_stops_mock_and_returns_failure(self):
        mock = Process()
        with patch.object(start_local, "assert_port_available"), \
             patch.object(start_local, "start_process", return_value=mock), \
             patch.object(start_local, "wait_for_health", side_effect=RuntimeError("mock unavailable")), \
             patch.object(sys, "argv", ["start_local.py"]):
            self.assertEqual(1, start_local.main())
        self.assertTrue(mock.terminated)
