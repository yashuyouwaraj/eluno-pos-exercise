import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SeedGenerationTests(unittest.TestCase):
    def test_generator_writes_complete_utf8_json_without_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "starter-kit"
            project.mkdir()
            generator = project / "generate_seed.py"
            generator.write_bytes((ROOT / "starter-kit" / "generate_seed.py").read_bytes())
            subprocess.run(
                [sys.executable, str(generator)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            data_dir = project / "data"
            files = list(data_dir.glob("*.json"))
            self.assertEqual(10, len(files))
            offers = json.loads((data_dir / "offers.json").read_text(encoding="utf-8"))
            self.assertIn("Priority customer ₹500 off", [offer["name"] for offer in offers])
            self.assertEqual([], list(data_dir.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
