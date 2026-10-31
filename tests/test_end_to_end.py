import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import support


class EndToEndTest(unittest.TestCase):
    def test_main_on_fixture_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template.vsdx"
            shutil.copy(support.FIXTURES / "template.vsdx", template)
            proc = subprocess.run(
                [sys.executable, str(support.SCRIPTS / "main.py"), str(support.FACTS), tmp,
                 f"--inventory={support.FIXTURES / 'inventory.yml'}", f"--template={template}"],
                capture_output=True, text=True, cwd=tmp,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("feature ipsec: ok", proc.stdout)

            root = ET.parse(Path(tmp) / "topology.drawio").getroot().find(".//root")
            layer = root.find("mxCell[@id='layer-ipsec']")
            self.assertIsNotNone(layer)
            self.assertEqual(layer.get("parent"), "0")
            tunnels = [o for o in root.findall("object") if o.get("id", "").startswith("ipsec-")]
            self.assertEqual(len(tunnels), 3)
            physical = [c for c in root.findall("mxCell") if c.get("edge") == "1"]
            self.assertEqual(len(physical), 5)
            vertex_ids = {c.get("id") for c in root.findall("mxCell") if c.get("vertex") == "1"
                          and c.get("parent") == "1"}
            for t in tunnels:
                cell = t.find("mxCell")
                self.assertIn(cell.get("source"), vertex_ids)
                self.assertIn(cell.get("target"), vertex_ids)

    @unittest.skipUnless(hasattr(sys, "stdlib_module_names"), "needs Python 3.10+")
    def test_no_third_party_imports(self):
        """Every import under scripts/ resolves to the stdlib or the project."""
        import ast
        local = {p.stem for p in support.SCRIPTS.glob("*.py")} | {"features", "drivers", "_hanan"}
        stdlib = set(sys.stdlib_module_names)
        for path in support.SCRIPTS.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    self.assertTrue(top in stdlib or top in local or top == "__future__",
                                    f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
