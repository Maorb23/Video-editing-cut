from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/video-editing-skill/skills/video-editing-skill"


class PackagingTests(unittest.TestCase):
    def test_manifests_and_required_skill_resources(self) -> None:
        plugin = json.loads((ROOT / "plugins/video-editing-skill/.codex-plugin/plugin.json").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "video-editing-skill")
        self.assertEqual(marketplace["plugins"][0]["source"]["path"], "./plugins/video-editing-skill")
        self.assertTrue((SKILL / "SKILL.md").is_file())
        self.assertTrue((SKILL / "assets/viewer.html").is_file())

    def test_every_script_defines_main_return_contract(self) -> None:
        for path in sorted((SKILL / "scripts").glob("*.py")):
            if path.name == "_bootstrap.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIn("main", text, path.name)
            self.assertIn("SystemExit(main())", text, path.name)

    def test_read_only_reference_repository_was_not_linked(self) -> None:
        forbidden = "manim" + "_skill"
        for path in ROOT.rglob("*.py"):
            self.assertNotIn(forbidden, path.read_text(encoding="utf-8"), str(path))


if __name__ == "__main__":
    unittest.main()
