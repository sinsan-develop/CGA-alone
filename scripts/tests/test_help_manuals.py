import importlib.util
from pathlib import Path
import unittest
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]

class HelpManualTests(unittest.TestCase):
    def load_builder(self):
        spec = importlib.util.spec_from_file_location("help_builder", ROOT / "docs/manual/build_web_manuals.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_generated_pages_match_sources(self):
        builder = self.load_builder()
        for name, source in builder.MANUALS.items():
            actual = (ROOT / "apps/web/public/manuals" / name).read_text(encoding="utf-8")
            self.assertEqual(actual, builder.build_page(source))
            self.assertIn('<html lang="ko">', actual)
            self.assertIn('name="viewport"', actual)
            self.assertIn('aria-label="목차"', actual)

    def test_content_semantics_and_safe_text(self):
        builder = self.load_builder()
        result = builder.render_markdown('# 제목\n\n<script>x</script> **강조** `a<b`\n\n1. 첫째\n2. 둘째\n\n| 종류 | 값 |\n|---|---|\n| A | B |\n')
        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt;', result)
        self.assertIn('<strong>강조</strong>', result)
        self.assertIn('<code>a&lt;b</code>', result)
        self.assertIn('<ol>', result)
        self.assertIn('<th scope="col">종류</th>', result)
        self.assertIn('<td>B</td>', result)

    def test_only_safe_external_links(self):
        builder = self.load_builder()
        result = builder.inline('[공식](https://example.com/docs) [위험](javascript:alert)')
        self.assertIn('href="https://example.com/docs"', result)
        self.assertNotIn('href="javascript:', result)

    def test_help_retains_originals_and_adds_both_manuals(self):
        rail = (ROOT / "apps/web/components/studio-rail.tsx").read_text(encoding="utf-8")
        for slug in ('getting-started', 'user-manual', 'nlu-guide'):
            self.assertIn('manualHref("' + slug + '")', rail)
        for name in ('cga-alone-guide-ko.html', 'channel-connections-ko.html'):
            self.assertIn('/manuals/' + name, rail)
        self.assertIn('t("nav.help")', rail)
        self.assertIn('openGettingStarted()', rail)

if __name__ == "__main__":
    unittest.main()
