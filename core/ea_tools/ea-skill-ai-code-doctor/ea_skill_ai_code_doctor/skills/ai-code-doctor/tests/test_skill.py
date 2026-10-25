from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze  # noqa: E402
import check_interface  # noqa: E402
import render_report  # noqa: E402


class AnalyzerTests(unittest.TestCase):
    def test_bom_python_file_is_analyzed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.py"
            path.write_bytes(b"\xef\xbb\xbfdef ok():\n    return 1\n")
            result = analyze.analyze_file(str(path))
        self.assertFalse(result["skipped"])
        self.assertEqual(result["function_count"], 1)

    def test_invalid_and_missing_inputs_return_structured_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.py"
            path.write_text("def broken(:\n", encoding="ascii")
            invalid = analyze.analyze_file(str(path))
        missing = analyze.analyze(str(path))
        self.assertTrue(invalid["skipped"])
        self.assertTrue(invalid["syntax_error"])
        self.assertEqual(missing["file_count"], 0)
        json.dumps(invalid)
        json.dumps(missing)

    def test_dependency_directories_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("def app():\n    return 1\n", encoding="ascii")
            for name in (".VENV", "site-packages", "node_modules", "__pycache__"):
                dependency = root / name
                dependency.mkdir()
                (dependency / "vendor.py").write_text("def vendor():\n    return 1\n", encoding="ascii")
            files = analyze.collect_files(str(root))
        self.assertEqual(files, [str(root / "app.py")])

    def test_hubs_use_qualified_names_and_ignore_recursion(self):
        source = """
class First:
    def save(self):
        return 1

class Second:
    def save(self):
        return 2

def call_first(value):
    return value.save()

def call_second(value):
    return value.save()

def recursive(value):
    if value:
        return recursive(value - 1)
    return 0

def call_recursive():
    return recursive(1)
"""
        tree = ast.parse(source)
        result = analyze.analyze_tree(tree, source, "fixture.py")
        self.assertEqual(result["hubs"], [])

    def test_real_module_and_method_hubs_are_detected(self):
        source = """
def helper():
    return 1

def first():
    return helper()

def second():
    return helper()

class Service:
    def helper(self):
        return 1

    def first(self):
        return self.helper()

    def second(self):
        return self.helper()
"""
        tree = ast.parse(source)
        result = analyze.analyze_tree(tree, source, "fixture.py")
        self.assertEqual([hub["name"] for hub in result["hubs"]], ["helper", "Service.helper"])

    def test_orm_chain_records_one_db_point(self):
        source = "def query():\n    return User.objects.all()\n"
        tree = ast.parse(source)
        result = analyze.analyze_tree(tree, source, "fixture.py")
        self.assertEqual(len(result["io_db_points"]), 1)
        self.assertEqual(result["io_db_points"][0]["call"], "all")


class InterfaceTests(unittest.TestCase):
    def _write(self, directory, name, source):
        path = Path(directory) / name
        path.write_text(source, encoding="utf-8")
        return str(path)

    def test_matching_interface_is_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            original = self._write(directory, "original.py", "def run(value, flag=False):\n    return value\n")
            optimized = self._write(
                directory,
                "optimized.py",
                "def run(value, flag=False):\n    return helper(value)\n\ndef helper(value):\n    return value + 1\n",
            )
            result = check_interface.compare_interfaces(original, optimized)
        self.assertTrue(result["compatible"])
        self.assertEqual([item["name"] for item in result["added"]], ["helper"])

    def test_removed_or_changed_interface_is_incompatible(self):
        with tempfile.TemporaryDirectory() as directory:
            original = self._write(
                directory,
                "original.py",
                "def run(value, flag=False):\n    return value\n\ndef gone():\n    return None\n",
            )
            optimized = self._write(directory, "optimized.py", "def run(value):\n    return value\n")
            result = check_interface.compare_interfaces(original, optimized)
        self.assertFalse(result["compatible"])
        self.assertEqual([item["name"] for item in result["missing"]], ["gone"])
        self.assertEqual([item["name"] for item in result["changed"]], ["run"])

    def test_changed_class_method_is_incompatible(self):
        with tempfile.TemporaryDirectory() as directory:
            original = self._write(
                directory,
                "original.py",
                "class Service:\n    def run(self, value):\n        return value\n",
            )
            optimized = self._write(
                directory,
                "optimized.py",
                "class Service:\n    def run(self):\n        return None\n",
            )
            result = check_interface.compare_interfaces(original, optimized)
        self.assertFalse(result["compatible"])
        self.assertEqual([item["name"] for item in result["changed"]], ["Service.run"])

    def test_bom_optimized_file_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            original = self._write(directory, "original.py", "def run():\n    return 1\n")
            optimized = Path(directory) / "optimized.py"
            optimized.write_bytes(b"\xef\xbb\xbfdef run():\n    return 2\n")
            result = check_interface.compare_interfaces(original, str(optimized))
        self.assertTrue(result["compatible"])


class ReportRendererTests(unittest.TestCase):
    def test_untrusted_report_fields_are_escaped(self):
        payload = "<script>alert('x')</script><img src=x onerror=alert(1)>"
        data = {
            "file_name": payload,
            "health_score": 55,
            "verdict": payload,
            "audit_date": "2026-07-20",
            "mode": "full",
            "findings": [{
                "tag": "smell",
                "severity": payload,
                "title": payload,
                "location": payload,
                "problem": payload,
                "impact": payload,
                "fix": payload,
                "gain": payload,
            }],
            "prerequisites": [payload],
            "optimized_code": payload,
            "diff_before": payload,
            "diff_after": payload,
            "tradeoffs": [payload],
        }
        template = (ROOT / "assets" / "report_template.html").read_text(encoding="utf-8")
        rendered = render_report.render_report(data, template)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotRegex(rendered, r"\{\{[A-Z0-9_]+\}\}")

    def test_runtime_prerequisites_are_required(self):
        data = {
            "file_name": "example.py",
            "health_score": 100,
            "verdict": "ok",
            "audit_date": "2026-07-20",
            "mode": "full",
            "findings": [],
            "prerequisites": [],
            "optimized_code": "",
            "diff_before": "",
            "diff_after": "",
            "tradeoffs": [],
        }
        template = (ROOT / "assets" / "report_template.html").read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "prerequisites must not be empty"):
            render_report.render_report(data, template)


if __name__ == "__main__":
    unittest.main()
