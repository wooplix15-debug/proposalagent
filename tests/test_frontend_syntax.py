import re
import shutil
import subprocess
import unittest
from pathlib import Path


class FrontendSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to check JavaScript syntax")
    def test_persistent_review_ui_script_parses(self):
        for filename in ("index.html", "workflow.html"):
            with self.subTest(page=filename):
                page = (Path(__file__).parents[1] / "public" / filename).read_text()
                scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
                result = subprocess.run(["node", "--check"], input="\n".join(scripts), text=True,
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
