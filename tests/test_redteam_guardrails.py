"""场景红队修复的回归测试(只测行为,不测措辞)。

对应 docs/eval_cases.md:
  #13 无意图/乱码 → 友好追问路由
  #9  字段名投毒 / #15 条件分支 / ⚠不硬猜 → 两条路径 prompt 均含安全边界铁律
"""
from __future__ import annotations

import pathlib

import pytest

from client.webui.pipeline import _looks_like_no_intent


@pytest.mark.parametrize("q", ["", "   ", "!!!@@@", "......", "∑®†¥¨ˆ", "、、、。。", "a"])
def test_no_intent_detected(q):
    assert _looks_like_no_intent(q) is True


@pytest.mark.parametrize("q", [
    "算一下业绩", "看看哪个产品卖得好", "forecast sales by region",
    "top 10 customers", "算 Q3 回款率", "按大区算回款率",
])
def test_real_query_not_flagged(q):
    assert _looks_like_no_intent(q) is False


def test_codegen_prompt_has_safety_rules():
    from client.webui.codegen import CODEGEN_SYSTEM
    # 字段名非指令 / 忽略注入 / 不支持条件分支 / 不硬猜
    for kw in ["不是给你的指令", "忽略之前的指令", "不支持条件分支", "不要硬猜"]:
        assert kw in CODEGEN_SYSTEM, f"codegen prompt 缺规则: {kw}"


def test_skill_prompt_has_safety_rules():
    sp = pathlib.Path(__file__).resolve().parents[1] / "docs" / "llm_system_prompt.md"
    text = sp.read_text(encoding="utf-8")
    for kw in ["不是指令", "忽略之前的指令", "不支持条件分支", "不要硬猜"]:
        assert kw in text, f"skill prompt 缺规则: {kw}"


def test_freechat_prompt_refuses_prompt_disclosure():
    # R9:闲聊路径也要拒绝复述系统提示词 + 忽略注入
    from client.webui.pipeline import _FREECHAT_SYSTEM
    assert "系统提示词" in _FREECHAT_SYSTEM and "忽略" in _FREECHAT_SYSTEM


# ── 优化循环 T0-1(财务场景评审)修复:预算差异档位色 + 方向歧义率不误染 ──

def test_budget_variance_tier_colors():
    from client.webui.writer import _tier_fill, _TIER_GOOD, _TIER_BAD
    def bg(v):
        r = _tier_fill(v)
        return r[0].fgColor.rgb[-6:] if r else None
    # 收入方向:超额/达标=好(绿),未达=坏(红);成本方向 超支=红/节约=绿
    assert bg("超额") == _TIER_GOOD[0] and bg("达标") == _TIER_GOOD[0]
    assert bg("未达") == _TIER_BAD[0] and bg("超支") == _TIER_BAD[0]
    assert bg("节约") == _TIER_GOOD[0]


def test_ambiguous_direction_rate_not_reverse_colored():
    from client.webui.writer import _AMBIGUOUS_DIRECTION, _REVERSE_METRIC
    # 差异率方向取决于成本/收入,不套逆向色阶(交给档位列),避免把收入超额染红
    assert "差异率" in _AMBIGUOUS_DIRECTION and "差异率" not in _REVERSE_METRIC


# ── 优化循环 T0-1b(财务同比环比评审)锁口径:增长额=金额、增长率=% ──

def test_yoy_mom_amount_vs_rate_format():
    from client.webui.writer import _infer_number_format, _MONEY_FMT
    # 增长额/差额是绝对金额 → 金额格式(红括号);增长率/率是百分比 → 0.00%
    for h in ("环比增长额", "同比增长额", "环比增长", "同比增长", "差异额"):
        assert _infer_number_format(h) == _MONEY_FMT, f"{h} 应为金额格式"
    for h in ("环比增长率", "同比增长率", "环比率", "同比率", "差异率"):
        assert _infer_number_format(h) == "0.00%", f"{h} 应为百分比"


# ── 优化循环 T0-2(库存场景评审)修复:ABC剔负值不溢出 + 周转数据异常隔离 ──

def _inv_frames():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    rows = [
        ("正常件A","IT", 55000, 400000),   # DIO~50 正常
        ("慢件B","办公", 96000, 240000),   # DIO~146 呆滞
        ("零动C","礼品", 20000, 0),        # 零销货成本 → 呆滞(NaN)
        ("退货冲减","耗材", -15000, 90000), # 负库存 → 数据异常
        ("退供件","耗材", 60000, -20000),  # 负销货成本 → 数据异常
    ]
    df = pd.DataFrame(rows, columns=["物料名称","类别","库存金额","销货成本"])
    meta = df[["物料名称","类别"]].to_dict("records")
    cdf = df[["库存金额","销货成本"]].reset_index(drop=True)
    return skills, cdf, meta


def test_pareto_abc_excludes_nonpositive_no_overflow():
    skills, cdf, meta = _inv_frames()
    _, d, _ = skills.run_skill("pareto_abc", cdf,
        {"label_col":"物料名称","value_col":"库存金额"}, meta, ["物料名称","类别"])
    pos = d[d["ABC类"] != "数据异常"]
    # 正值累计占比不得突破 100%
    assert pos["累计占比"].max() <= 1.0 + 1e-9
    # 负值行被隔离为「数据异常」,占比/累计留空,不混入 ABC
    bad = d[d["ABC类"] == "数据异常"]
    assert len(bad) == 1 and bad["占比"].isna().all() and bad["累计占比"].isna().all()


def test_inventory_turnover_anomaly_blank_and_bottom():
    import numpy as np
    skills, cdf, meta = _inv_frames()
    _, d, _ = skills.run_skill("inventory_turnover", cdf,
        {"item_col":"物料名称","stock_col":"库存金额","cogs_col":"销货成本","days":365},
        meta, ["物料名称","类别"])
    # 数据异常行:周转天数置空(不显示无意义负数)
    bad = d[d["库存状态"] == "数据异常"]
    assert len(bad) == 2 and bad["周转天数"].isna().all()
    # 数据异常置于表尾
    assert (d["库存状态"] == "数据异常").iloc[-len(bad):].all()
    # 零销货成本的呆滞(NaN=不周转)排在最前
    assert d.iloc[0]["库存状态"] == "呆滞" and np.isnan(d.iloc[0]["周转天数"])


# ── 优化循环 T0-3(客户场景评审)修复:账龄不静默丢负账龄/NaN,合计对平总账 ──

def test_ar_aging_reconciles_negative_and_nan_age():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    rows = [
        ("A", 180000, 0),      # 当期
        ("A", 30000, -10),     # 负账龄=未到期(旧实现会被 pd.cut 静默丢)
        ("B", 50000, 45),      # 逾期
        ("C", 20000, None),    # 账龄缺失(金额有效)
    ]
    df = pd.DataFrame(rows, columns=["客户","应收","账龄"])
    meta = df[["客户"]].to_dict("records")
    cdf = df[["应收","账龄"]].reset_index(drop=True)
    _, d, _ = skills.run_skill("ar_aging", cdf,
        {"amount_col":"应收","age_col":"账龄","group_col":"客户"}, meta, ["客户"])
    raw = df["应收"].sum()
    # 账龄表合计必须与应收总账对平(不因负账龄/NaN 静默丢数据)
    assert abs(d["合计"].sum() - raw) < 1e-6
    # 负账龄进「未到期」桶
    assert "未到期" in d.columns and d[d["客户"]=="A"]["未到期"].iloc[0] == 30000
    # NaN 账龄进「账龄未知」桶,不吞金额
    assert "账龄未知" in d.columns and d[d["客户"]=="C"]["账龄未知"].iloc[0] == 20000


def test_ar_aging_no_extra_cols_when_all_normal():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    df = pd.DataFrame([("A",100000,10),("A",50000,40)], columns=["客户","应收","账龄"])
    _, d, _ = skills.run_skill("ar_aging", df[["应收","账龄"]].reset_index(drop=True),
        {"amount_col":"应收","age_col":"账龄","group_col":"客户"},
        df[["客户"]].to_dict("records"), ["客户"])
    # 无未到期 / 无缺失账龄 → 兜底列不出现,报表不添空列
    assert "未到期" not in d.columns and "账龄未知" not in d.columns


def test_rfm_segments_vip_and_churn():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    import random
    rows = []
    for i in range(20):
        rows.append((f"客户{i}", 3+i*4, 60-i*2, 3000000-i*120000))  # 递减:前=优后=差
    df = pd.DataFrame(rows, columns=["客户","最近","频次","金额"])
    _, d, _ = skills.run_skill("rfm_segment", df[["最近","频次","金额"]].reset_index(drop=True),
        {"customer_col":"客户","recency_col":"最近","frequency_col":"频次","monetary_col":"金额"},
        df[["客户"]].to_dict("records"), ["客户"])
    # 最优客户(recency最小/频次金额最高)= 重要价值;最差 = 流失
    assert d[d["客户"]=="客户0"]["分群"].iloc[0] == "重要价值客户"
    assert d[d["客户"]=="客户19"]["分群"].iloc[0] == "流失客户"


# ── 优化循环 T0-4(HR场景评审)新增:人效技能 + 绩效分级并列同档 ──

def test_per_capita_weighted_total_and_headcount():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    rows = [
        ("A部","张",1000000),("A部","李",800000),          # A: 2人 共180万 → 人均90万
        ("B部","王",600000),("B部","赵",600000),("B部","孙",600000),  # B: 3人 共180万 → 人均60万
    ]
    df = pd.DataFrame(rows, columns=["部门","姓名","产出"])
    _, d, _ = skills.run_skill("per_capita", df[["产出"]].reset_index(drop=True),
        {"group_col":"部门","value_col":"产出","name_col":"姓名","metric_name":"人均产出"},
        df[["部门","姓名"]].to_dict("records"), ["部门","姓名"])
    a = d[d["部门"]=="A部"].iloc[0]
    assert a["人数"] == 2 and abs(a["人均产出"] - 900000) < 1e-6
    # 合计人均 = 总额360万 ÷ 总人数5 = 72万(加权),不是(90+60)/2=75万
    tot = d[d["部门"]=="合计"].iloc[0]
    assert tot["人数"] == 5 and abs(tot["人均产出"] - 720000) < 1e-6
    # 排序:人均降序,A部(90万)在 B部(60万)前
    order = list(d[d["部门"]!="合计"]["部门"])
    assert order.index("A部") < order.index("B部")


def test_per_capita_dedup_headcount():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    # 同一人多行记录 → 去重人头只算 1 人
    rows = [("A部","张",500000),("A部","张",300000),("A部","李",200000)]
    df = pd.DataFrame(rows, columns=["部门","姓名","产出"])
    _, d, _ = skills.run_skill("per_capita", df[["产出"]].reset_index(drop=True),
        {"group_col":"部门","value_col":"产出","name_col":"姓名"},
        df[["部门","姓名"]].to_dict("records"), ["部门","姓名"])
    a = d[d["部门"]=="A部"].iloc[0]
    assert a["人数"] == 2  # 张(2行)+李 = 2 人头


def test_hr_grade_ties_same_grade():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    # 同分员工必须同档(rank average),不因行序给不同档
    rows = [("张",80),("李",80),("王",80),("赵",50),("孙",90)]
    df = pd.DataFrame(rows, columns=["姓名","绩效"])
    _, d, _ = skills.run_skill("hr_grade", df[["绩效"]].reset_index(drop=True),
        {"name_col":"姓名","metric_col":"绩效"}, df[["姓名"]].to_dict("records"), ["姓名"])
    g = dict(zip(d["姓名"], d["绩效等级"]))
    assert g["张"] == g["李"] == g["王"]  # 三个 80 分同档


# ── 优化循环 T0-5(密态边界):同态近零噪声去噪(纯函数,无需密钥)──

def test_denoise_he_snaps_near_zero():
    import pandas as pd
    from client.tools.skills import _denoise_he
    # 模拟 HE 解密结果:精确 0→1e-15、真实值带 1e-13 抖动、身份列不动
    df = pd.DataFrame({
        "金额": [1e-15, 100.0, 2500.5000000000002, -3e-16],
        "名称": ["甲", "乙", "丙", "丁"],
    })
    out = _denoise_he(df.copy())
    assert out["金额"].iloc[0] == 0.0 and out["金额"].iloc[3] == 0.0   # 近零归零
    assert out["金额"].iloc[1] == 100.0                                # 正常值不动
    assert list(out["名称"]) == ["甲", "乙", "丙", "丁"]               # 非数值列不动


def test_denoise_he_preserves_small_real_values_above_eps():
    import pandas as pd
    from client.tools.skills import _denoise_he
    # 阈值 1e-6 以上的真实小值必须保留(不误杀)
    df = pd.DataFrame({"x": [0.01, 1e-5, 1e-3]})
    out = _denoise_he(df.copy())
    assert (out["x"] == pd.Series([0.01, 1e-5, 1e-3])).all()


# ── 优化循环 T1-1(真实LLM验证):R3 加密数值列不做字符串模糊匹配规则 ──

def test_codegen_prompt_forbids_string_match_on_encrypted_numeric():
    from client.webui.codegen import CODEGEN_SYSTEM
    # 规则要点:数值列不做字符串/模糊匹配、改数值范围
    assert "字符串" in CODEGEN_SYSTEM and "数值范围" in CODEGEN_SYSTEM
    assert "str.contains" in CODEGEN_SYSTEM or "模糊匹配" in CODEGEN_SYSTEM


def test_skill_prompt_forbids_string_match_on_encrypted_numeric():
    import pathlib
    sp = pathlib.Path(__file__).resolve().parents[1] / "docs" / "llm_system_prompt.md"
    text = sp.read_text(encoding="utf-8")
    assert "数值范围" in text and "模糊匹配" in text


# ── 优化循环 T1-2:降序表负值/异常不置顶(亏损行曾坐降序表首行) ──

def test_codegen_prompt_forbids_hoisting_negatives_to_top():
    from client.webui.codegen import CODEGEN_SYSTEM
    # 要点:NaN 沉底(na_position=last)、禁止 na_position=first、禁止把异常行拼到表头
    assert 'na_position="last"' in CODEGEN_SYSTEM
    assert 'na_position="first"' in CODEGEN_SYSTEM   # 以「禁止」形式出现
    assert "置顶" in CODEGEN_SYSTEM and "误导" in CODEGEN_SYSTEM


# ── 优化循环第10轮(补货红队):单行结果表百分数列体检失效 → 显示错100倍 ──

def test_single_row_integer_percent_not_rendered_100x():
    import pandas as pd, openpyxl
    from client.webui import writer
    # 单行汇总表,完成率存的是整数百分数 85(=85%)。旧实现因 len>=2 跳过体检,
    # 套 0.00% 会渲染成 8500%。
    df = pd.DataFrame([("华东", 85.0)], columns=["大区", "完成率"])
    p = writer.write_skill_results([{"sheet_name": "t", "df": df}], stem="pct1row", staging=True)
    ws = openpyxl.load_workbook(p)["t"]
    c = ws.cell(2, 2)
    if not isinstance(c.value, (int, float)):
        c = ws.cell(3, 2)
    # 整数百分数 → 用字面 % 格式(不再乘100)
    assert c.number_format != "0.00%", "单行整数百分数被当小数,会显示成 8500%"
    assert c.value == 85


def test_single_row_true_decimal_ratio_still_percent():
    import pandas as pd, openpyxl
    from client.webui import writer
    # 单行 1.6 = 160% 超额完成,是合法小数,不能被误判缩小 100 倍
    df = pd.DataFrame([("华东", 1.6)], columns=["大区", "完成率"])
    p = writer.write_skill_results([{"sheet_name": "t", "df": df}], stem="pct1row_dec", staging=True)
    ws = openpyxl.load_workbook(p)["t"]
    c = ws.cell(2, 2)
    if not isinstance(c.value, (int, float)):
        c = ws.cell(3, 2)
    assert c.number_format == "0.00%" and abs(c.value - 1.6) < 1e-9
