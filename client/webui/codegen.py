"""
代码生成 + 安全执行引擎(SKILL.md 架构)。

流程:
  ① build_codegen_messages —— 把相关 SKILL.md 教学内容 + schema + 问题组装成 LLM 提示
  ② extract_code           —— 从 LLM 回复里抠 ```python``` 代码块
  ③ ast_safety_check       —— AST 扫描,拒危险调用 / import / dunder 逃逸
  ④ run_generated_code     —— 受限 exec:
        - 只暴露 ps / ct / hp / hl / pd / np + cdf + metadata + results
        - 自定义 __import__,把 crypto_toolkit 换成"解密门控代理"
        - 首次 ct.decrypt* → 触发 B6-1 授权(prompt_decrypt)
        - 代码把结果写进 results = [{sheet_name, df, chart}]

安全模型:同进程受限 exec。威胁是"LLM 写错/越界代码",不是恶意用户
(密钥本就在本机)。受限 builtins + import 白名单 + AST 扫描 + dunder 拦截。
"""

from __future__ import annotations

import ast
import re
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# 信号异常
# ---------------------------------------------------------------------------

class CodegenCancelled(Exception):
    """用户在 B6-1 授权门点了取消 / 停止。"""


class KeepEncrypted(Exception):
    """用户在 B6-1 授权门选了「保留密文」—— 不解密,导出源密文。"""


class UnsafeCode(Exception):
    """AST 安全扫描发现危险构造。"""


# ---------------------------------------------------------------------------
# ① 组装代码生成提示
# ---------------------------------------------------------------------------

CODEGEN_SYSTEM = """你是同态加密数据分析的代码生成助手。

你将根据下方提供的 SKILL.md 技能文档,**编写一段 Python 代码**来完成用户的分析需求。

═══════════ 执行环境(已为你准备好,禁止重复) ═══════════
以下变量 / 模块在执行命名空间里**已就绪**,直接用,不要写 import,不要写
hp.initDict() / ct.initSK()(已初始化):
  - cdf               :已加载的 CipherDataFrame(用户的密文数据)
  - metadata_rows     :list[dict] 明文身份列(姓名 / 大区 / 月份 等)
  - metadata_columns  :list[str]  身份列名
  - ps  = pandaseal   ct = crypto_toolkit   hp = henumpy   hl = helearn
  - pd  = pandas      np = numpy

═══════════ 你的代码必须遵守 ═══════════
1. 不要写 import 语句,不要写初始化(环境已就绪)。
2. 把最终**已解密**的结果表写进 `results` 列表,每个元素:
     {"sheet_name": "中文表名", "df": <pandas.DataFrame 明文>, "chart": {...}|None}
   chart 可选,格式 {"type":"bar"|"line", "x":"列名", "y":"列名"|["列1","列2"], "title":"标题"}
3. 解密用 ct.decrypt_df(cdf) / ct.decrypt(...);**首次解密会触发用户授权**,
   是正常流程,直接调用即可。
4. 身份列合并:解密后的 df 行序与 metadata_rows 一致,可
     meta = pd.DataFrame(metadata_rows)[[c for c in metadata_columns]]
     full = pd.concat([meta.reset_index(drop=True), decrypted.reset_index(drop=True)], axis=1)
5. 派生指标用 pandas/numpy 算(如 回款率 = 回款金额 / 实际销售额)。
6. 禁止:文件读写(open)、os/sys/subprocess/socket、网络、eval/exec、访问 __ 开头属性。

═══════════ 输出格式 ═══════════
先写一段 ```python ... ``` 代码块(只一段,自包含),
再写 <summary>给用户看的中文说明,零明文(不含具体数值/姓名/日期)</summary>。
"""


def build_codegen_messages(
    skill_docs: list,
    schema: dict,
    metadata_columns: list[str],
    user_query: str,
    custom_block: str = "",
) -> tuple[str, str]:
    """返回 (system, user)。skill_docs 是 SkillDoc 列表。"""
    import json

    # 拼 SKILL.md 教学内容(正文 + INDEX,examples 取前 1 个免 token 爆)
    skill_blocks = []
    for d in skill_docs:
        block = [f"════════ SKILL: {d.name} ════════", d.body]
        if d.index_md:
            block.append(f"\n── {d.name} · API 索引(INDEX.md) ──\n{d.index_md}")
        if d.examples:
            block.append(f"\n── {d.name} · 示例 ──\n{d.examples[0]}")
        skill_blocks.append("\n".join(block))
    skills_text = "\n\n".join(skill_blocks)

    system = CODEGEN_SYSTEM
    if custom_block:
        system = system + "\n" + custom_block

    user = (
        f"可用技能文档:\n{skills_text}\n\n"
        f"═══════════ 用户数据 schema(只有字段名,无明文) ═══════════\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        f"身份列(明文):{metadata_columns}\n\n"
        f"═══════════ 用户问题 ═══════════\n{user_query}\n\n"
        f"请按上面的执行环境约定写代码(用 cdf,把结果放进 results),再给 summary。"
    )
    return system, user


# ---------------------------------------------------------------------------
# ② 抽代码块
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_SUMMARY_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)


def extract_code(text: str) -> tuple[str, str]:
    """从 LLM 回复抠 (code, summary)。"""
    if not text or not text.strip():
        raise ValueError("LLM 返回空文本")
    m = _CODE_RE.search(text)
    if not m:
        raise ValueError("LLM 回复里没有 ```python``` 代码块")
    code = m.group(1).strip()

    sm = _SUMMARY_RE.search(text)
    summary = sm.group(1).strip() if sm else ""
    if not summary:
        # 代码块后面的自由文本兜底
        after = text.split("```", 2)
        if len(after) >= 3:
            summary = after[2].strip()[:600]
    if not summary:
        summary = "已按需求生成分析,详见 Excel。"
    return code, summary


# ---------------------------------------------------------------------------
# ③ AST 安全扫描
# ---------------------------------------------------------------------------

# 允许 import 的模块(以及别名)
_ALLOWED_IMPORTS = {
    "henumpy", "pandaseal", "crypto_toolkit", "helearn", "hetorch",
    "pandas", "numpy", "math", "datetime", "re", "json",
    "statistics", "collections", "itertools", "functools",
}

# 禁止调用的内建名
_BANNED_CALLS = {
    "eval", "exec", "compile", "open", "input", "__import__",
    "globals", "locals", "vars", "breakpoint", "delattr",
    "setattr", "memoryview", "help", "exit", "quit",
}


def ast_safety_check(code: str) -> None:
    """对生成代码做 AST 扫描,发现危险构造抛 UnsafeCode。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise UnsafeCode(f"代码语法错误: {e}") from e

    for node in ast.walk(tree):
        # import 白名单
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _ALLOWED_IMPORTS:
                    raise UnsafeCode(f"禁止 import「{alias.name}」")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top not in _ALLOWED_IMPORTS:
                raise UnsafeCode(f"禁止 from「{node.module}」import")
        # 危险调用
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _BANNED_CALLS:
                raise UnsafeCode(f"禁止调用「{fn.id}」")
            if isinstance(fn, ast.Attribute) and fn.attr in _BANNED_CALLS:
                raise UnsafeCode(f"禁止调用「.{fn.attr}」")
        # dunder 逃逸(().__class__.__bases__ 之类)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                # 放行常见安全 dunder
                if node.attr not in ("__len__", "__name__"):
                    raise UnsafeCode(f"禁止访问 dunder 属性「.{node.attr}」")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") and node.id.endswith("__") and node.id != "__name__":
                raise UnsafeCode(f"禁止使用 dunder 名「{node.id}」")


# ---------------------------------------------------------------------------
# ④ 受限执行
# ---------------------------------------------------------------------------

# crypto_toolkit 解密门控代理 —— 首次 decrypt 触发 B6-1
class _CtGate:
    def __init__(self, real_ct, on_first_decrypt: Callable[[], None]):
        object.__setattr__(self, "_ct", real_ct)
        object.__setattr__(self, "_on_first", on_first_decrypt)
        object.__setattr__(self, "_authorized", False)

    def __getattr__(self, name):
        attr = getattr(object.__getattribute__(self, "_ct"), name)
        if name in ("decrypt", "decrypt_df", "decrypt_ndarray", "decrypt_csv"):
            def wrapped(*a, **k):
                if not object.__getattribute__(self, "_authorized"):
                    object.__getattribute__(self, "_on_first")()  # 可能 raise
                    object.__setattr__(self, "_authorized", True)
                return attr(*a, **k)
            return wrapped
        return attr


# 安全 builtins 子集
def _safe_builtins(allowed_import_fn):
    import builtins as _b
    safe_names = [
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
        "float", "format", "frozenset", "int", "isinstance", "issubclass",
        "len", "list", "map", "max", "min", "next", "print", "range", "repr",
        "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
        "type", "zip", "True", "False", "None", "abs", "bytes", "complex",
        "hasattr", "getattr",
    ]
    d = {n: getattr(_b, n) for n in safe_names if hasattr(_b, n)}
    d["__import__"] = allowed_import_fn
    # 常见异常类
    for exc in ("Exception", "ValueError", "TypeError", "KeyError", "IndexError",
                "ZeroDivisionError", "RuntimeError", "StopIteration", "ArithmeticError"):
        if hasattr(_b, exc):
            d[exc] = getattr(_b, exc)
    return d


def run_generated_code(
    code: str,
    *,
    cdf,
    metadata_rows: list[dict],
    metadata_columns: list[str],
    prompt_decrypt: Optional[Callable[[], str]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> list[dict]:
    """
    受限 exec 生成代码,返回 results 列表 [{sheet_name, df, chart}]。
    decrypt 首次调用触发 B6-1。
    """
    import numpy as np
    import pandas as pd

    from client.tools.runtime import Runtime
    Runtime.get().ensure_all_initialized()
    import crypto_toolkit as ct
    import henumpy as hp
    import pandaseal as ps
    try:
        import helearn as hl
    except Exception:
        hl = None

    # B6-1 门控回调
    def _on_first_decrypt():
        if should_cancel and should_cancel():
            raise CodegenCancelled("用户已停止")
        decision = "decrypt"
        if prompt_decrypt:
            decision = prompt_decrypt() or "decrypt"
        if decision == "cancel":
            raise CodegenCancelled("用户已停止")
        if decision == "keep_encrypted":
            raise KeepEncrypted("用户选择保留密文")
        # decrypt → 放行

    ct_gate = _CtGate(ct, _on_first_decrypt)

    # 自定义 import:只放白名单,crypto_toolkit 换成门控代理
    real_modules = {
        "henumpy": hp, "pandaseal": ps, "crypto_toolkit": ct_gate,
        "helearn": hl, "pandas": pd, "numpy": np,
    }
    import importlib

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".")[0]
        if top not in _ALLOWED_IMPORTS:
            raise UnsafeCode(f"运行时禁止 import「{name}」")
        if name in real_modules and real_modules[name] is not None:
            return real_modules[name]
        return importlib.import_module(name)

    results: list[dict] = []
    sandbox_globals: dict[str, Any] = {
        "__builtins__": _safe_builtins(_guarded_import),
        "cdf": cdf,
        "metadata_rows": metadata_rows,
        "metadata_columns": metadata_columns,
        "ps": ps, "ct": ct_gate, "hp": hp, "hl": hl,
        "pd": pd, "np": np,
        "results": results,
    }

    compiled = compile(code, "<generated_skill_code>", "exec")
    exec(compiled, sandbox_globals)  # noqa: S102 —— 受限命名空间 + AST 扫描

    # 取回 results(代码可能重新赋值 results = [...])
    final = sandbox_globals.get("results", results)
    if not isinstance(final, list):
        raise ValueError("生成代码没有产出 results 列表")
    # 规整:只留 {sheet_name, df, chart}
    cleaned: list[dict] = []
    for r in final:
        if not isinstance(r, dict):
            continue
        df = r.get("df")
        if df is None:
            continue
        cleaned.append({
            "sheet_name": r.get("sheet_name") or "结果",
            "df": df,
            "chart": r.get("chart"),
        })
    return cleaned
