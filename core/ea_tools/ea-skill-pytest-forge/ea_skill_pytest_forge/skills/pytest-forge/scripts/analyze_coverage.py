from __future__ import annotations

"""
测试覆盖率盲区分析器（pytest-forge 子能力）
科大讯飞 AI 数据智能分析与应用 Skill 开发挑战赛 · 方向二：测试与质量保障。

输入：Python 源码文件 + （可选的）现有测试文件/目录
输出：盲区分析报告 report.md + 可直接运行的缺口测试骨架 test_<模块>_gaps.py

与 generate_tests.py 形成「组合拳」：
  1) generate_tests.py 从零铺量（为所有函数生成冒烟测试）
  2) analyze_coverage.py 查漏补缺（找出现有测试中未覆盖的函数/方法，按风险排序，并自动为缺口补骨架）

设计原则：高鲁棒性
- 主路径用 coverage.py 实测行覆盖；coverage 不可用或测试运行失败时自动降级为静态名称匹配
- 未提供任何测试 → 视为全部盲区（等价于提示用 generate_tests.py 从零生成），不崩溃
- 复用 generate_tests.build_test_file 生成缺口骨架，保证语法永远合法、可运行
- 所有异常被吞掉并给出可读提示，工具自身零崩溃
"""
import ast
import json
import os
import sys
import argparse
import subprocess
import datetime
import traceback

# 加载 generate_tests 模块（pytest 生成器）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GEN_PATH = os.path.join(SCRIPT_DIR, "generate_tests.py")
_spec = __import__("importlib.util").util.spec_from_file_location("utg_generate", GEN_PATH)
gen = __import__("importlib.util").util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

SPECIAL_DUNDERS = {"__str__", "__len__", "__repr__", "__eq__", "__getitem__"}


# --------------------------------------------------------------------------- #
# 单元提取（分支 / 复杂度 / __init__）
# --------------------------------------------------------------------------- #
def count_branches(node):
    """统计分支/决策点数量，作为复杂度权重。"""
    c = 0
    for n in ast.walk(node):
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While,
                          ast.With, ast.AsyncWith, ast.Try, ast.ExceptHandler,
                          ast.IfExp, ast.BoolOp, ast.comprehension, ast.Assert)):
            c += 1
    return c


def make_unit(node, qual, kind, parent=None):
    name = node.name
    exposure = 0.4 if (name.startswith("_") and not name.startswith("__")) else 1.0
    return {
        "qual": qual,
        "name": name,
        "kind": kind,
        "parent": parent,
        "node": node,
        "async": isinstance(node, ast.AsyncFunctionDef),
        "params": list(gen.iter_params(node)),
        "start": node.lineno,
        "end": getattr(node, "end_lineno", node.lineno),
        "branches": count_branches(node),
        "exposure": exposure,
    }


def is_testable_method(m):
    if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if m.name == "__init__":
        return True
    if m.name.startswith("_") and m.name not in SPECIAL_DUNDERS:
        return False
    return True


def extract_units(tree):
    units = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append(make_unit(n, qual=n.name, kind="func"))
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_testable_method(m):
                    units.append(make_unit(m, qual="%s.%s" % (n.name, m.name),
                                           kind="method", parent=n.name))
    return units


# --------------------------------------------------------------------------- #
# 测试发现 / 覆盖分类
# --------------------------------------------------------------------------- #
def find_test_files(tests_path):
    """返回测试文件路径列表；tests_path 可为文件或目录。"""
    if tests_path is None:
        return []
    if os.path.isfile(tests_path):
        return [tests_path]
    if os.path.isdir(tests_path):
        files = []
        for fn in sorted(os.listdir(tests_path)):
            if fn.startswith("test_") and fn.endswith(".py"):
                files.append(os.path.join(tests_path, fn))
        return files
    return []


def read_test_source(files):
    chunks = []
    for f in files:
        try:
            src, _ = gen.read_source(f)
            chunks.append(src)
        except Exception:
            continue
    return "\n".join(chunks)


def coverage_run(src_abs, tests_path, out_dir):
    """用 coverage.py（Python API）精确测量单个源码文件的行覆盖。

    Key points:
    - Use `include=[src_abs]` to lock onto the file under test and avoid pulling
      sibling files (possibly containing syntax errors) into coverage.
    - Run `cov.start()` -> `pytest.main()` -> `cov.stop()` in-process, avoiding
      the subprocess-discovery semantics of `coverage run -m pytest`; most accurate.

    Returns (executed_set, missing_set) or None (downgraded by caller on failure).
    """
    try:
        import coverage
        cov = coverage.Coverage(include=[src_abs])
        cov.start()
        try:
            import pytest
        except ImportError:
            cov.stop()
            return None
        rc = pytest.main([tests_path, "-q", "-p", "no:cacheprovider"])
        cov.stop()
        data = cov.get_data()
        # normcase 归一化（Windows 大小写不敏感）
        key = None
        for mf in data.measured_files():
            if os.path.normcase(os.path.abspath(mf)) == os.path.normcase(src_abs):
                key = mf
                break
        if key is None:
            return None
        executed = set(data.lines(key) or [])
        missing = set()
        try:
            missing = set(cov.analysis2(key)[3])
        except Exception:
            missing = set()
        return executed, missing
    except Exception:
        return None


def classify(units, mode, src_abs, tests_path, test_src, out_dir):
    """
    返回 (covered_map, ratio_map, mode_used)
    covered_map: qual -> bool
    ratio_map:   qual -> float (0~1)
    """
    cov_result = None
    mode_used = mode
    if mode in ("auto", "coverage"):
        cov_result = coverage_run(src_abs, tests_path, out_dir)
        if cov_result is not None:
            mode_used = "coverage"
        elif mode == "coverage":
            # coverage 失败 → 标注模式（全盲区）
            mode_used = "coverage(失败→全盲区)"

    if cov_result is not None:
        executed, _ = cov_result
        covered = {}
        ratio = {}
        for u in units:
            # 排除 def 签名行（import 时也会执行，不算覆盖）
            body = set(range(u["start"], u["end"] + 1))
            if u["end"] > u["start"]:
                body.discard(u["start"])
            if body:
                hit = body & executed
                covered[u["qual"]] = len(hit) > 0
                ratio[u["qual"]] = len(hit) / max(1, len(body))
            else:
                # 单行 def 无可执行 body（import 时不产生行命中）
                # 回退为静态名称匹配
                name_hit = u["name"] in test_src
                covered[u["qual"]] = name_hit
                ratio[u["qual"]] = 1.0 if name_hit else 0.0
        return covered, ratio, mode_used

    # 静态降级（名称匹配）
    mode_used = "static(名称匹配)" if mode_used != "coverage(失败→全盲区)" else mode_used
    covered = {}
    ratio = {}
    for u in units:
        token = u["name"]
        covered[u["qual"]] = token in test_src
        ratio[u["qual"]] = 1.0 if token in test_src else 0.0
    return covered, ratio, mode_used


# --------------------------------------------------------------------------- #
# 缺口骨架（复用 generate_tests.build_test_file）
# --------------------------------------------------------------------------- #
def build_gap_targets(units, tree, gap_quals):
    targets = []
    # 先处理顶层函数
    for u in units:
        if u["kind"] == "func" and u["qual"] in gap_quals:
            targets.append(gen.make_func_target(u["node"]))
    # 方法按 parent 类归组
    seen = set()
    for u in units:
        if u["kind"] == "method" and u["qual"] in gap_quals:
            parent = u["parent"]
            if parent in seen:
                continue
            seen.add(parent)
            class_node = next((n for n in tree.body
                               if isinstance(n, ast.ClassDef) and n.name == parent), None)
            if class_node is None:
                continue
            ct = gen.make_class_target(class_node)
            ct["methods"] = [m for m in ct["methods"]
                             if ("%s.%s" % (parent, m["name"])) in gap_quals]
            targets.append(ct)
    return targets


# --------------------------------------------------------------------------- #
# &
# --------------------------------------------------------------------------- #
def priority_score(u, ratio):
    return (1.0 - ratio) * (1.0 + 0.15 * u["branches"]) * u["exposure"]


def prio_tag(score):
    if score >= 2.0:
        return "🔴 高"
    if score >= 1.0:
        return "🟡 中"
    return "🟢 低"


def reason_text(u, ratio):
    parts = ["覆盖率 %.0f%%" % (ratio * 100)]
    if u["branches"] >= 3:
        parts.append("分支复杂(%d)" % u["branches"])
    elif u["branches"] > 0:
        parts.append("%d branches" % u["branches"])
    else:
        parts.append("no branches")
    parts.append("公开API" if u["exposure"] >= 1.0 else "内部方法")
    return ", ".join(parts)


def write_report(path, stats, units, covered_map, ratio_map, blind_sorted,
                 gap_test_rel, mode_used, no_tests, tests_path):
    L = []
    L.append("# Test Coverage Blind-Spot Analysis Report (pytest-forge)")
    L.append("")
    L.append("> Placeholder: coverage analysis summary follows.")
    L.append("")
    L.append("## 概述")
    L.append("")
    L.append("- **源码文件**：`%s`" % stats["input"])
    L.append("- **Existing tests**: %s" % ("`%s`" % tests_path if tests_path else "none provided (all blind spots)"))
    L.append("- **判定模式**：%s" % mode_used)
    L.append("- **Testable units**: %d (functions %d / methods %d)"
             % (stats["units"], stats["funcs"], stats["methods"]))
    cov_n = sum(1 for u in units if covered_map[u["qual"]])
    L.append("- **已覆盖**：%d、**未覆盖（盲区）**：%d、**单元覆盖率**：%.0f%%"
             % (cov_n, len(units) - cov_n, (cov_n / max(1, len(units)) * 100)))
    L.append("- **Truncated**: %s"""
             % ("yes (over %d units, took first %d)" % (gen.MAX_FUNCS, gen.MAX_FUNCS) if stats["truncated"] else "no"))
    L.append("")

    # 明细表
    L.append("## 逐项覆盖明细")
    L.append("")
    L.append("| 单元 | 类型 | 行区间 | 分支数 | 覆盖率 | 状态 |")
    L.append("|------|------|--------|--------|--------|------|")
    for u in units:
        typ = "函数" if u["kind"] == "func" else "方法"
        rng = "%d-%d" % (u["start"], u["end"])
        st = "covered" if covered_map[u["qual"]] else "blind-spot"
        L.append("| `%s` | %s | %s | %d | %.0f%% | %s |"
                 % (u["qual"], typ, rng, u["branches"], ratio_map[u["qual"]] * 100, st))
    L.append("")

    # 优先级表
    L.append("## 盲区补测优先级（按风险排序）")
    L.append("")
    if not blind_sorted:
        L.append("🎉 未发现盲区，现有测试已覆盖全部可测试单元。")
    else:
        L.append("| 优先级 | 单元 | 风险等级 | 评分 | 说明 |")
        L.append("|--------|------|----------|------|------|")
        for i, u in enumerate(blind_sorted, 1):
            L.append("| #%d | `%s` | %s | %.2f | %s |"
                     % (i, u["qual"], prio_tag(u["score"]), u["score"],
                        reason_text(u, ratio_map[u["qual"]])))
        L.append("")
        L.append("> 评分 = (1 − 覆盖率) × (1 + 0.15×分支数) × 公开度权重（公开 1.0 / 内部 0.4）。"
                 "分值越高越应优先补测。")
    L.append("")

    # 缺口骨架
    L.append("## 缺口测试骨架")
    L.append("")
    if gap_test_rel:
        L.append("- 已自动生成可直接运行的缺口骨架：`%s`" % gap_test_rel)
        L.append("- 该文件复用与 `generate_tests.py` 相同的骨架模板（happy + boundary 冒烟测试），"
                 "开发者只需在 `TODO` 处补充业务断言即可。")
    else:
        L.append("- 无盲区，无需生成缺口骨架。")
    L.append("")

    L.append("## 鲁棒性说明")
    L.append("")
    L.append("- 覆盖判定优先采用 `coverage.py` 实测行覆盖；不可用时自动降级为静态名称匹配，绝不崩溃")
    L.append("- 未提供测试文件时，全部单元判为盲区并提示可用 `generate_tests.py` 从零生成")
    L.append("- 缺口骨架复用 `generate_tests.build_test_file`，保证语法合法、可运行、导入失败则 skip")
    L.append("- 单文件单元上限 %d，超出截断并在本报告提示" % gen.MAX_FUNCS)
    L.append("")
    L.append("---")
    L.append("生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# --------------------------------------------------------------------------- #
#  Main flow

# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="测试覆盖率盲区分析器")
    ap.add_argument("-i", "--input", required=True, help="Python 源码文件路径")
    ap.add_argument("-t", "--tests", default=None,
                    help="现有测试文件或目录（可选；省略则全部视为盲区）")
    ap.add_argument("-o", "--out", default="./coverage_report", help="输出目录（默认 ./coverage_report）")
    ap.add_argument("--mode", choices=["auto", "coverage", "static"], default="auto",
                    help="覆盖判定模式（默认 auto：优先 coverage，失败降级 static）")
    args = ap.parse_args()

    src_path = os.path.abspath(args.input)
    if not os.path.isfile(src_path):
        print("[ERROR] 输入文件不存在: %s" % src_path, file=sys.stderr)
        return 2

    # -t 未指定时，尝试同目录 test_<模块>.py
    tests_path = os.path.abspath(args.tests) if args.tests else None
    no_tests = False
    if not tests_path:
        stem = os.path.splitext(os.path.basename(src_path))[0]
        cand = os.path.join(os.path.dirname(src_path), "test_%s.py" % stem)
        if os.path.isfile(cand):
            tests_path = cand
        else:
            no_tests = True

    os.makedirs(args.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    report_path = os.path.join(args.out, "report.md")
    gap_test_path = os.path.join(args.out, "test_%s_gaps.py" % stem)

    stats = {"input": src_path, "units": 0, "funcs": 0, "methods": 0,
             "truncated": False}

    try:
        source, enc = gen.read_source(src_path)
    except Exception as e:
        print("[ERROR] 读取源码失败: %s" % e, file=sys.stderr)
        return 2

    try:
        tree = ast.parse(source, filename=src_path)
    except SyntaxError as e:
        # 语法错误 → 直接写报告
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# 测试覆盖率盲区分析报告\n\n"
                    "> 源码存在语法错误（行 %s）：%s。无法解析提取单元，请先修复源码。\n"
                    % (e.lineno, e.msg))
        print("[ERROR] 源码语法错误，无法分析: %s" % e, file=sys.stderr)
        return 2

    units = extract_units(tree)
    if len(units) > gen.MAX_FUNCS:
        units = units[:gen.MAX_FUNCS]
        stats["truncated"] = True
    stats["units"] = len(units)
    stats["funcs"] = sum(1 for u in units if u["kind"] == "func")
    stats["methods"] = sum(1 for u in units if u["kind"] == "method")

    test_files = [] if no_tests else find_test_files(tests_path)
    test_src = read_test_source(test_files)

    try:
        covered_map, ratio_map, mode_used = classify(
            units, args.mode, src_path, tests_path, test_src, args.out)
    except Exception as e:
        print("[WARN] 覆盖判定异常，降级为全盲区: %s" % e, file=sys.stderr)
        covered_map = {u["qual"]: False for u in units}
        ratio_map = {u["qual"]: 0.0 for u in units}
        mode_used = "异常→全盲区"

    gap_quals = set(u["qual"] for u in units if not covered_map[u["qual"]])
    blind = [u for u in units if not covered_map[u["qual"]]]
    for u in blind:
        u["score"] = priority_score(u, ratio_map[u["qual"]])
    blind_sorted = sorted(blind, key=lambda u: -u["score"])

    # （自动生成缺口骨架）
    gap_test_rel = None
    if gap_quals:
        try:
            gap_targets = build_gap_targets(units, tree, gap_quals)
            gen.build_test_file(gap_targets, src_path, gap_test_path)
            gap_test_rel = os.path.basename(gap_test_path)
        except Exception as e:
            print("[WARN] 缺口骨架生成异常: %s" % traceback.format_exc(), file=sys.stderr)
            gap_test_rel = None

    write_report(report_path, stats, units, covered_map, ratio_map, blind_sorted,
                 gap_test_rel, mode_used, no_tests, tests_path)

    print("[OK] 报告: %s" % report_path)
    if gap_test_rel:
        print("[OK] 缺口骨架: %s" % gap_test_path)
    print("[INFO] 单元 %d（函数 %d / 方法 %d）｜已覆盖 %d｜盲区 %d｜模式 %s"
          % (stats["units"], stats["funcs"], stats["methods"],
             sum(1 for u in units if covered_map[u["qual"]]),
             len(units) - sum(1 for u in units if covered_map[u["qual"]]),
             mode_used))
    return 0


if __name__ == "__main__":
    sys.exit(main())
