"""CLI JSON must work even on a Windows machine without a Chinese locale."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CLIEncodingTests(unittest.TestCase):
    def test_all_entrypoints_return_utf8_json_under_ascii_stdio(self):
        with tempfile.TemporaryDirectory(prefix="sme-cli-locale-") as td:
            base = Path(td).resolve()
            workspace = base / "中文工作目录"
            env = dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0")

            def run(script, *args, cwd=ROOT):
                completed = subprocess.run([sys.executable, "-B", str(script), *map(str, args)],
                    cwd=cwd, env=env, capture_output=True, encoding="utf-8")
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)

            run(ROOT / "install.py", "install", "--skills-dir", base / "skills")
            run(ROOT / "runtime/bootstrap.py", "init", "--workspace", workspace,
                "--scope", "both", "--operator", "模拟中文操作人")
            self.assertEqual(run(ROOT / "runtime/bootstrap.py", "doctor", "--workspace", workspace)["status"], "healthy")
            run(workspace / "human_review.py", "export", "--out", "exports", cwd=workspace)
            run(workspace / "monitor.py", "export", "--profile", "profile.json",
                "--db", "data/monitor.sqlite", "--out", "exports", cwd=workspace)
            run(ROOT / "tools/profile_bundle.py", "pack", "--profile", workspace / "profile.json",
                "--out", base / "模拟画像.zip")


if __name__ == "__main__":
    unittest.main()
