"""
密态边界回归 —— 用**真实 HE 后端**跑关键技能,验证口径在同态噪声下仍成立。

跑法:AGENT_BACKEND=real,且本机有密钥 vault(~/.agent-system/keystore/<user>/vault/
含 sk.bin/evk.bin/user_authorization)。缺密钥的机器整模块自动 skip,不拖累常规 CI。

重点覆盖(优化循环 T0-5):
- 同态解密把精确 0 解成 ~1e-15 → 击穿 `replace(0, NaN)` 除零护栏(零分母炸成天文数字)。
  _decrypt 在解密边界吸附近零噪声,下列断言确认修复成立。
- 加权比率 / 人效 / 库存 DIO 等口径在真实 HE 下与明文一致(容差内)。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_backend


def _find_vault() -> Path | None:
    root = Path.home() / ".agent-system" / "keystore"
    if not root.is_dir():
        return None
    for user_dir in root.iterdir():
        v = user_dir / "vault"
        if (v / "sk.bin").is_file() and (v / "evk.bin").is_file():
            return v
    return None


@pytest.fixture(scope="module")
def real_skills():
    """绑定真实密钥 vault 并初始化 HE;缺密钥或非 real 后端 → skip 整模块。"""
    if os.environ.get("AGENT_BACKEND", "stub").lower() != "real":
        pytest.skip("需 AGENT_BACKEND=real")
    vault = _find_vault()
    if vault is None:
        pytest.skip("本机无密钥 vault(sk.bin/evk.bin),跳过真实后端口径测试")
    from client.tools import runtime as rt
    rt.set_active_vault(str(vault))
    try:
        rt.Runtime.get().ensure_all_initialized()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"HE 初始化失败(密钥/授权问题):{e}")
    from client.tools import skills
    return skills


def _enc(df):
    import crypto_toolkit as ct
    return ct.encrypt_df(df)


def test_he_roundtrip_zero_snaps_to_zero(real_skills):
    import pandas as pd
    df = pd.DataFrame({"a": [0.0, 100.0, 2500.5], "b": [1.0, 2.0, 3.0]})
    back = real_skills._decrypt(_enc(df))
    # 精确 0 经 HE 解密后被吸附回 0(而非 1e-15)
    assert back["a"].iloc[0] == 0.0
    # 非零值在容差内一致
    assert abs(back["a"].iloc[2] - 2500.5) < 1e-6


def test_inventory_zero_cogs_blank_under_real(real_skills):
    import pandas as pd
    rows = [("零动件", 88000, 0), ("正常件", 55000, 400000)]
    df = pd.DataFrame(rows, columns=["物料名称", "库存金额", "销货成本"])
    _, d, _ = real_skills.run_skill(
        "inventory_turnover", _enc(df[["库存金额", "销货成本"]]),
        {"item_col": "物料名称", "stock_col": "库存金额", "cogs_col": "销货成本", "days": 365},
        df[["物料名称"]].to_dict("records"), ["物料名称"])
    z = d[d["物料名称"] == "零动件"]
    # 零销货成本:周转天数留空(不因 1e-15 噪声炸成天文数字)、判呆滞
    assert pd.isna(z["周转天数"].iloc[0])
    assert z["库存状态"].iloc[0] == "呆滞"
    assert z["销货成本"].iloc[0] == 0.0


def test_weighted_ratio_matches_plaintext_under_real(real_skills):
    import pandas as pd
    rows = [("华东", 90, 100), ("华东", 80, 100), ("华南", 50, 200)]
    df = pd.DataFrame(rows, columns=["大区", "回款", "应收"])
    _, d, _ = real_skills.run_skill(
        "ratio_by_group", _enc(df[["回款", "应收"]]),
        {"num_col": "回款", "den_col": "应收", "group_col": "大区", "metric_name": "回款率"},
        df[["大区"]].to_dict("records"), ["大区"])
    east = d[d["大区"] == "华东"]["回款率"].iloc[0]
    assert abs(east - 0.85) < 1e-6   # 170/200 加权


def test_per_capita_matches_plaintext_under_real(real_skills):
    import pandas as pd
    rows = [("A部", "张", 1000000), ("A部", "李", 800000), ("B部", "王", 600000)]
    df = pd.DataFrame(rows, columns=["部门", "姓名", "产出"])
    _, d, _ = real_skills.run_skill(
        "per_capita", _enc(df[["产出"]]),
        {"group_col": "部门", "value_col": "产出", "name_col": "姓名", "metric_name": "人均产出"},
        df[["部门", "姓名"]].to_dict("records"), ["部门", "姓名"])
    a = d[d["部门"] == "A部"]["人均产出"].iloc[0]
    assert abs(a - 900000) < 1e-3   # 180万/2
