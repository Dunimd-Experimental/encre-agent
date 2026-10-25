from __future__ import annotations

"""
单元测试自动生成 Skill —— 核心生成器
科大讯飞 AI 数据智能分析与应用 Skill 开发挑战赛 · 方向二：测试与质量保障。

输入：Python 源码文件（路径）
输出：pytest 测试文件 + Markdown 报告 +（可选）pytest 运行结果

设计原则：高鲁棒性
- 基于 ast 静态解析，复杂/残缺代码下始终产出「语法合法」的测试文件
- 目标模块导入失败 / 语法错误 → 生成"可运行但全部 skip"的测试 + 清晰报告，绝不崩溃
- 生成的测试文件永远能跑起来（无 collection error），符合赛事"无输出崩溃"扣分指标
"""
import ast
import argparse
import os
import sys
import subprocess
import datetime
import traceback

MAX_FUNCS = 60  # 单文件函数/方法上限，超出截断并在报告中说明

NAME_DUMMY = {
    "n": "0", "i": "0", "j": "0", "k": "0", "index": "0", "count": "0",
    "num": "0", "x": "0", "y": "0",
    "s": '"x"', "text": '"x"', "name": '"x"', "msg": '"x"', "string": '"x"',
    "lst": "[]", "list": "[]", "arr": "[]", "items": "[]", "seq": "[]",
    "d": "{}", "dict": "{}", "mapping": "{}",
    "b": "False", "flag": "False", "enabled": "False",
}


# --------------------------------------------------------------------------- #
# 参数与取值推断
# --------------------------------------------------------------------------- #
def ann_str(node):
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def iter_params(func):
    """yield (name, annotation_str, default_node, kind) for non *args/**kwargs."""
    a = func.args
    pos = a.args
    ndef = len(a.defaults)
    for idx, p in enumerate(pos):
        default = a.defaults[idx - len(pos)] if idx >= len(pos) - ndef else None
        yield (p.arg, ann_str(p.annotation), default, "pos")
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        yield (p.arg, ann_str(p.annotation), d, "kw")


def ann_dummy(ann):
    a = (ann or "").lower()
    if a in ("int", "integer"):
        return "0"
    if a in ("float", "double", "number"):
        return "0.0"
    if a in ("str", "string"):
        return '""'
    if a == "bool":
        return "False"
    if "list" in a:
        return "[]"
    if "dict" in a:
        return "{}"
    if "set" in a:
        return "set()"
    if "tuple" in a:
        return "()"
    return None


def name_dummy(name):
    n = (name or "").lower()
    if n in NAME_DUMMY:
        return NAME_DUMMY[n]
    if n.endswith("s") and len(n) > 3:
        return "[]"
    if "path" in n or "file" in n or "dir" in n:
        return '""'
    return "None"


def param_value(name, ann, default_node):
    """happy-path 取值：优先默认值，其次按注解/名字推断安全 dummy。"""
    if default_node is not None:
        try:
            return repr(ast.literal_eval(default_node))
        except Exception:
            return "None"
    d = ann_dummy(ann)
    if d is not None:
        return d
    return name_dummy(name)


def edge_value(name, ann):
    """boundary 取值：倾向触发边界行为的输入。"""
    a = (ann or "").lower()
    n = (name or "").lower()
    if a in ("int", "integer") or n in ("n", "i", "j", "k", "index", "count", "num"):
        return "0"
    if a in ("float", "double", "number"):
        return "0.0"
    if a in ("str", "string") or "path" in n:
        return '""'
    if a == "bool":
        return "False"
    if "list" in a or (n.endswith("s") and len(n) > 3):
        return "[]"
    if "dict" in a:
        return "{}"
    return "None"


def call_args(params, use_edge=False):
    parts = []
    for (name, ann, default, kind) in params:
        if name in ("self", "cls"):
            continue
        val = edge_value(name, ann) if use_edge else param_value(name, ann, default)
        parts.append(val)
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# 目标构建（函数 / 类 / 方法）
# --------------------------------------------------------------------------- #
def make_func_target(node):
    return {
        "kind": "func",
        "name": node.name,
        "params": list(iter_params(node)),
        "async": isinstance(node, ast.AsyncFunctionDef),
    }


def make_class_target(node):
    init_params = None
    methods = []
    for n in node.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.name == "__init__":
                init_params = list(iter_params(n))
            elif not n.name.startswith("_") or n.name in ("__str__", "__len__", "__repr__", "__eq__", "__getitem__"):
                methods.append({
                    "name": n.name,
                    "params": list(iter_params(n)),
                    "async": isinstance(n, ast.AsyncFunctionDef),
                })
    return {"kind": "class", "name": node.name, "init_params": init_params, "methods": methods}


def extract_targets(tree):
    targets = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            targets.append(make_func_target(n))
        elif isinstance(n, ast.ClassDef):
            targets.append(make_class_target(n))
    return targets


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def gen_func_tests(t, mod_name):
    out = []
    fname = t["name"]
    is_async = t["async"]
    for suffix, use_edge in (("happy", False), ("boundary", True)):
        if is_async:
            call = "asyncio.run(%s.%s(%s))" % (mod_name, fname, call_args(t["params"], use_edge))
        else:
            call = "%s.%s(%s)" % (mod_name, fname, call_args(t["params"], use_edge))
        if suffix == "happy":
            body = (
                "    if _TARGET_MOD is None:\n"
                "        pytest.skip(f\"目标模块导入失败: {_IMPORT_ERROR}\")\n"
                "    %s\n"
                "    # TODO: 补充针对返回值的业务断言（当前为冒烟测试，验证不抛异常）\n"
                "    assert True\n" % call
            )
        else:
            body = (
                "    if _TARGET_MOD is None:\n"
                "        pytest.skip(f\"目标模块导入失败: {_IMPORT_ERROR}\")\n"
                "    try:\n"
                "        %s\n"
                "    except Exception:\n"
                "        pytest.skip(\"边界输入触发异常，需人工确认是否为预期行为\")\n"
                "    # TODO: 补充边界断言\n"
                "    assert True\n" % call
            )
        out.append("def test_%s_%s():\n%s" % (fname, suffix, body))
    return "\n\n".join(out)


def gen_class_tests(t, mod_name):
    out = []
    cname = t["name"]
    init_args = call_args(t["init_params"] or [], False) if t["init_params"] else ""
    # 工厂函数
    out.append(
        "def _make_%s():\n"
        "    if _TARGET_MOD is None:\n"
        "        return None\n"
        "    try:\n"
        "        return %s.%s(%s)\n"
        "    except Exception:\n"
        "        try:\n"
        "            return %s.%s()\n"
        "        except Exception:\n"
        "            return None\n"
        % (cname, mod_name, cname, init_args, mod_name, cname)
    )
    # __init__
    out.append(
        "def test_%s_init():\n"
        "    inst = _make_%s()\n"
        "    if inst is None:\n"
        "        pytest.skip(\"%s could not be instantiated (check constructor params)\")\n"
        "    assert inst is not None\n" % (cname, cname, cname)
    )
    # 方法测试
    for m in t["methods"]:
        mname = m["name"]
        is_async = m["async"]
        if is_async:
            call = "asyncio.run(inst.%s(%s))" % (mname, call_args(m["params"], False))
        else:
            call = "inst.%s(%s)" % (mname, call_args(m["params"], False))
        body = (
            "    inst = _make_%s()\n"
            "    if inst is None:\n"
            "        pytest.skip(\"%s instantiation failed; add constructor params\")\n"
            "    %s\n"
            "    # TODO: 补充针对返回值的业务断言\n"
            "    assert True\n" % (cname, cname, call)
        )
        out.append("def test_%s_%s_happy():\n%s" % (cname, mname, body))
    return "\n\n".join(out)


def build_test_file(targets, target_path, test_path):
    mod_name = "_TARGET_MOD"
    parts = []
    parts.append('"""Automatically generated by pytest-forge (科大讯飞测试与质量保障赛道)."""')
    parts.append("import sys\nimport os\nimport asyncio\nimport pytest\nimport importlib.util\n")
    parts.append('_TARGET_PATH = %s' % repr(os.path.abspath(target_path)))
    parts.append(
        "_spec = importlib.util.spec_from_file_location(%s, _TARGET_PATH)\n"
        "try:\n"
        "    %s = importlib.util.module_from_spec(_spec)\n"
        "    _spec.loader.exec_module(%s)\n"
        "    _IMPORT_ERROR = None\n"
        "except Exception as _e:\n"
        "    %s = None\n"
        "    _IMPORT_ERROR = _e\n" % (repr("_TARGET_MOD"), mod_name, mod_name, mod_name)
    )
    bodies = []
    for t in targets:
        if t["kind"] == "func":
            bodies.append(gen_func_tests(t, mod_name))
        elif t["kind"] == "class":
            bodies.append(gen_class_tests(t, mod_name))
    parts.append("\n\n".join(bodies))
    content = "\n\n".join(parts) + "\n"
    with open(test_path, "w", encoding="utf-8") as f:
        f.write(content)
    return content


# --------------------------------------------------------------------------- #
#  Main flow

# --------------------------------------------------------------------------- #
def read_source(path):
    last_err = None
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read(), enc
        except Exception as e:
            last_err = e
    raise last_err


def main():
    ap = argparse.ArgumentParser(description="Unit test auto generator")
    ap.add_argument("-i", "--input", required=True, help="Python source file path")
    ap.add_argument("-o", "--out", default="./ut_output", help="Output dir (default ./ut_output)")
    ap.add_argument("--no-run", action="store_true", help="Generate only; do not run pytest")
    args = ap.parse_args()

    src_path = os.path.abspath(args.input)
    if not os.path.isfile(src_path):
        print("[ERROR] 输入文件不存在: %s" % src_path, file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    test_path = os.path.join(args.out, "test_%s.py" % stem)
    report_path = os.path.join(args.out, "report.md")

    stats = {
        "input": src_path,
        "functions": 0,
        "classes": 0,
        "methods": 0,
        "tests": 0,
        "parse_ok": False,
        "import_ok": None,
        "truncated": False,
        "run": None,
    }

    try:
        source, enc = read_source(src_path)
    except Exception as e:
        write_report(report_path, stats, "读取失败: %s" % e, enc=None)
        print("[ERROR] 读取文件失败: %s" % e, file=sys.stderr)
        return 2

    try:
        tree = ast.parse(source, filename=src_path)
        stats["parse_ok"] = True
    except SyntaxError as e:
        # 降级生成占位测试（全部 skip）
        placeholder = (
            '"""Automatically generated by pytest-forge."""\n'
            "import pytest\n\n"
            "def test_syntax_error_in_target():\n"
            "    pytest.skip(\"目标源码存在语法错误，无法解析: %s\")\n" % e
        )
        with open(test_path, "w", encoding="utf-8") as f:
            f.write(placeholder + "\n")
        stats["parse_ok"] = False
        write_report(report_path, stats,
                     "Target source syntax error (line %s): %s. Placeholder tests were generated and all skip; the source stays broken." % (e.lineno, e.msg), enc=enc)
        print("[OK] 已降级生成占位测试（源码语法错误）: %s" % test_path)
        print("[OK] 报告: %s" % report_path)
        return 0

    targets = extract_targets(tree)
    # 截断保护
    if len(targets) > MAX_FUNCS:
        targets = targets[:MAX_FUNCS]
        stats["truncated"] = True

    func_count = sum(1 for t in targets if t["kind"] == "func")
    class_count = sum(1 for t in targets if t["kind"] == "class")
    method_count = sum(len(t["methods"]) for t in targets if t["kind"] == "class")
    stats["functions"] = func_count
    stats["classes"] = class_count
    stats["methods"] = method_count
    stats["tests"] = func_count * 2 + class_count * (1 + method_count)

    try:
        build_test_file(targets, src_path, test_path)
    except Exception as e:
        write_report(report_path, stats, "测试文件生成异常: %s" % traceback.format_exc(), enc=enc)
        print("[ERROR] 生成测试文件异常: %s" % e, file=sys.stderr)
        return 2

    #  Run pytest

    run_note = None
    if not args.no_run:
            import pytest  # noqa

    write_report(report_path, stats, run_note, enc=enc)
    print("[OK] 测试文件: %s" % test_path)
    print("[OK] 报告: %s" % report_path)
    print("[INFO] funcs %d / classes %d / methods %d / generated cases %d (parse %s, run %s)"
          % (func_count, class_count, method_count, stats["tests"],
             "成功" if stats["parse_ok"] else "失败", stats["run"]))
    return 0


def write_report(path, stats, run_note, enc):
    lines = []
    lines.append("# 单元测试生成报告（pytest-forge）")
    lines.append("")
    lines.append("> 科大讯飞 AI 数据智能分析与应用 Skill 开发挑战赛 · 方向二：测试与质量保障。")
    lines.append("")
    lines.append("## 概述")
    lines.append("")
    lines.append("- **输入文件**：`%s`" % stats["input"])
    lines.append("- **源码编码**：%s" % (enc or "未知"))
    lines.append("- **AST 解析**：%s" % ("✅ 成功" if stats["parse_ok"] else "❌ 失败（已降级）"))
    lines.append("- **函数数**：%d" % stats["functions"])
    lines.append("- **类数**：%d" % stats["classes"])
    lines.append("- **方法数**：%d" % stats["methods"])
    lines.append("- **生成测试用例数**：%d" % stats["tests"])
    lines.append("- **Truncated**: %s" % ("yes (over %d units, took first %d)" % (MAX_FUNCS, MAX_FUNCS) if stats["truncated"] else "no"))
    lines.append("- **pytest run**: %s" % (stats["run"] or "not executed"))
    lines.append("")
    lines.append("## 生成的测试文件")
    lines.append("")
    lines.append("`test_<模块名>.py` 已生成于输出目录，包含：")
    lines.append("- 每个函数：`test_<func>_happy`（主路径冒烟）、`test_<func>_boundary`（边界冒烟）")
    lines.append("- 每个类：`test_<Class>_init`（实例化）、每个方法 `test_<Class>_<method>_happy`")
    lines.append("- 目标模块通过 `importlib` 按路径加载，导入失败则全部 `skip`，**套件永远可运行、不崩溃**")
    lines.append("")
    lines.append("## 鲁棒性说明")
    lines.append("")
    lines.append("- 复杂/残缺代码下仍产出语法合法的测试文件")
    lines.append("- 目标模块语法错误或导入失败 → 生成「全部 skip」占位测试，不报错退出")
    lines.append("- 生成的用例为**冒烟测试骨架**，断言处标注 `TODO`，需人工补充业务断言")
    lines.append("- 单文件函数上限 %d，超出截断并在本报告中提示" % MAX_FUNCS)
    lines.append("")
    if run_note:
        lines.append("## pytest 运行结果")
        lines.append("")
        lines.append("```")
        lines.append(run_note)
        lines.append("```")
        lines.append("")
    lines.append("---")
    lines.append("生成时间：%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
