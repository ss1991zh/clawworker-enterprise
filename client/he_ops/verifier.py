"""
运行时校验器(Verifier)—— Phase 3「明文小样本对拍校验」的落地件。

在把 LLM 生成的代码放到**全量密文**上跑(慢)之前,先用一份**合成的小样本**(按 schema 造的
几行随机数据)走一遍同样的密态链路:加密 → 跑生成代码 → 解密 → 体检结果。
能在几毫秒内抓出:列名写错、类型/逻辑崩溃、结果结构不对、NaN/inf 爆炸 —— 早失败、早重规划。

隐私:合成样本是**本机随机造的**、用用户自己的密钥加密、只在本机解密体检,**不出本机、不给 LLM**。
这是"冒烟/契约"校验(随机输入无已知答案),目的是"代码能不能稳定跑通并产出像样结果",
而非数值正确性 oracle(数值正确性靠 he_ops 的算子级对拍保证)。

用法(库内 / 未来接入 pipeline):
    from client.he_ops.verifier import verify
    verdict = verify(code, numeric_cols=["销售额","回款额"], identity_cols=["大区"])
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class Verdict:
    ok: bool
    error: str = ""
    sheets: int = 0
    issues: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"通过 · 产出 {self.sheets} 张表" + (f" · 提示:{'; '.join(self.issues)}" if self.issues else "")
        return f"未通过 · {self.error}"


def _synthetic_cdf(numeric_cols: Sequence[str], n: int):
    """按列名造一份小的明文样本并加密成 CipherDataFrame;返回 (cdf, metadata_rows 用的 df 行数)。"""
    import numpy as np
    import pandas as pd
    import crypto_toolkit as ct

    cols = list(numeric_cols) or ["value"]
    data = {c: np.round(np.random.uniform(1.0, 100.0, n), 2) for c in cols}
    ndf = pd.DataFrame(data)
    return ct.encrypt_df(ndf)


def verify(code: str,
           numeric_cols: Sequence[str],
           identity_cols: Sequence[str] = (),
           n: int = 5) -> Verdict:
    """对生成代码做合成小样本冒烟校验。任何异常都收敛成 Verdict(不抛)。"""
    from client.tools.runtime import Runtime
    from client.webui.codegen import (
        ast_safety_check, run_generated_code, UnsafeCode,
        CodegenCancelled, KeepEncrypted, DecryptionFailed,
    )

    try:
        ast_safety_check(code)
    except UnsafeCode as e:
        return Verdict(False, error=f"安全扫描未过:{e}")

    try:
        Runtime.get().ensure_all_initialized()
        cdf = _synthetic_cdf(numeric_cols, n)
        meta_rows = [{c: f"{c}{i % 3}" for c in identity_cols} for i in range(n)]
        results = run_generated_code(
            code, cdf=cdf,
            metadata_rows=meta_rows, metadata_columns=list(identity_cols),
            prompt_decrypt=lambda: "decrypt",   # 合成数据,自动放行解密
        )
    except (CodegenCancelled, KeepEncrypted):
        return Verdict(True, issues=["代码选择了取消/保留密文(逻辑可达)"])
    except DecryptionFailed as e:
        return Verdict(False, error=f"解密失败:{e}")
    except Exception as e:  # noqa: BLE001 —— 生成代码崩溃 = 校验未过
        return Verdict(False, error=f"{type(e).__name__}: {e}")

    return _inspect(results)


def _inspect(results) -> Verdict:
    import numpy as np
    import pandas as pd

    if not results:
        return Verdict(False, error="代码没有产出任何结果(results 为空)")
    issues: list[str] = []
    sheets = 0
    for i, item in enumerate(results):
        if not isinstance(item, dict) or "df" not in item:
            return Verdict(False, error=f"第{i+1}个结果缺少 df / 结构不对")
        df = item["df"]
        if not isinstance(df, pd.DataFrame):
            return Verdict(False, error=f"第{i+1}个结果的 df 不是 DataFrame")
        sheets += 1
        if df.empty:
            issues.append(f"表「{item.get('sheet_name', i+1)}」为空")
        num = df.select_dtypes(include=[np.number])
        if num.size and not np.all(np.isfinite(num.to_numpy(dtype=float))):
            issues.append(f"表「{item.get('sheet_name', i+1)}」含 NaN/inf")
    return Verdict(True, sheets=sheets, issues=issues)
