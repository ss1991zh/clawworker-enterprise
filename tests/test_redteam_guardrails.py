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


# ── 优化循环第11轮(补货P3场景评审):透视表非加性聚合口径 ──

def _pivot_frames():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    rows = [("华东", "伺服", 300000), ("华东", "伺服", 200000), ("华东", "传感", 120000),
            ("华北", "传感", 160000), ("华北", "电源", 140000)]   # 华东无电源/华北无伺服
    df = pd.DataFrame(rows, columns=["大区", "产品线", "销售额"])
    return skills, df[["销售额"]].reset_index(drop=True), df[["大区", "产品线"]].to_dict("records"), df


def test_pivot_mean_total_is_overall_mean_not_sum_of_means():
    skills, cdf, meta, df = _pivot_frames()
    _, d, _ = skills.run_skill("pivot_summary", cdf,
        {"row_col": "大区", "col_col": "产品线", "value_col": "销售额", "agg": "mean"},
        meta, ["大区", "产品线"])
    # 合计列改名「总体均值」,且=原始行整体均值(不是各列均值相加)
    assert "总体均值" in d.columns and "合计" not in d.columns
    east = d[d["大区"] == "华东"]["总体均值"].iloc[0]
    assert abs(east - (300000 + 200000 + 120000) / 3) < 1e-6
    # 各列均值之和(370000)是错的,必须不等于它
    assert abs(east - 370000) > 1


def test_pivot_nonadditive_missing_combo_blank_not_zero():
    import pandas as pd
    skills, cdf, meta, df = _pivot_frames()
    _, d, _ = skills.run_skill("pivot_summary", cdf,
        {"row_col": "大区", "col_col": "产品线", "value_col": "销售额", "agg": "min"},
        meta, ["大区", "产品线"])
    # 华东没有「电源」→ 留空,不能填 0(会被读成"最低卖了0元")
    assert pd.isna(d[d["大区"] == "华东"]["电源"].iloc[0])
    # 总体最小 = 该大区所有行的最小值
    assert d[d["大区"] == "华东"]["总体最小"].iloc[0] == 120000


def test_pivot_sum_unchanged_zero_fill_and_total():
    skills, cdf, meta, df = _pivot_frames()
    _, d, _ = skills.run_skill("pivot_summary", cdf,
        {"row_col": "大区", "col_col": "产品线", "value_col": "销售额", "agg": "sum"},
        meta, ["大区", "产品线"])
    # 加性口径:缺失=0 合理,合计仍是求和
    assert d[d["大区"] == "华东"]["电源"].iloc[0] == 0
    assert d[d["大区"] == "华东"]["合计"].iloc[0] == 620000


# ── 优化循环第12轮(补货P3):行级均分母/TOP-N并列/口径说明齐备 ──

def test_row_ratio_mean_exposes_valid_row_count():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda c: c.copy()
    # 华东5单,2单应收为0(比率NaN不进均值)→ 必须显性列出「有效行数」,否则均值×订单数对不上账
    rows = [("华东", 90, 100), ("华东", 80, 100), ("华东", 70, 0),
            ("华东", 60, 0), ("华东", 50, 100)]
    df = pd.DataFrame(rows, columns=["大区", "回款", "应收"])
    _, d, _ = skills.run_skill("row_ratio_then_group_mean", df[["回款", "应收"]].reset_index(drop=True),
        {"num_col": "回款", "den_col": "应收", "group_col": "大区", "metric_name": "平均回款率"},
        df[["大区"]].to_dict("records"), ["大区"])
    r = d.iloc[0]
    assert r["订单数"] == 5 and r["有效行数"] == 3
    # 均值 × 有效行数 必须可对平
    assert abs(r["平均回款率"] * r["有效行数"] - (0.9 + 0.8 + 0.5)) < 1e-9


def test_row_ratio_no_noise_column_when_all_valid():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda c: c.copy()
    df = pd.DataFrame([("华南", 40, 100), ("华南", 30, 100)], columns=["大区", "回款", "应收"])
    _, d, _ = skills.run_skill("row_ratio_then_group_mean", df[["回款", "应收"]].reset_index(drop=True),
        {"num_col": "回款", "den_col": "应收", "group_col": "大区"},
        df[["大区"]].to_dict("records"), ["大区"])
    assert "有效行数" not in d.columns   # 无剔除时不添噪声列


def test_top_n_includes_boundary_ties():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda c: c.copy()
    df = pd.DataFrame([("A", 100), ("B", 90), ("C", 90), ("D", 90), ("E", 50)],
                      columns=["产品", "销售额"])
    _, d, _ = skills.run_skill("top_n_by", df[["销售额"]].reset_index(drop=True),
        {"value_col": "销售额", "n": 2}, df[["产品"]].to_dict("records"), ["产品"])
    # 并列的 90 全部带上(不能只留 B 丢掉 C/D),且共享名次 2
    assert set(d["产品"]) == {"A", "B", "C", "D"}
    assert list(d[d["销售额"] == 90]["排名"]) == [2, 2, 2]


def test_all_skills_have_caliber_note():
    from client.tools.skills import SKILLS
    missing = [k for k, v in SKILLS.items() if not (v.get("note") or "").strip()]
    assert not missing, f"这些技能缺口径说明(渲染在表顶供财务核对): {missing}"


def test_forecast_note_declares_no_seasonality():
    from client.tools.skills import SKILLS
    note = SKILLS["forecast_linreg"]["note"]
    assert "季节" in note and "仅供参考" in note   # 线性外推的能力边界必须写明


# ── 优化循环第13轮(补货:渲染层与新增列一致性)──

def test_aging_unknown_bucket_is_money_not_days():
    from client.webui.writer import _infer_number_format
    # 「账龄未知」是账龄分桶列,装的是**金额**;不能因含"账龄"被判成天数格式 0.0
    assert _infer_number_format("账龄未知") != "0.0"
    assert _infer_number_format("未到期") != "0.0"
    # 真正的天数列仍按天数渲染
    assert _infer_number_format("应收账龄(天)") == "0.0"
    assert _infer_number_format("周转天数") == "0.0"


def test_skill_note_renders_at_sheet_top():
    import pandas as pd, openpyxl
    from client.tools import skills
    from client.webui import writer
    skills._decrypt = lambda c: c.copy()
    df = pd.DataFrame([("A", 180000, 0), ("B", 50000, 45)], columns=["客户", "应收", "账龄"])
    s, d, c = skills.run_skill("ar_aging", df[["应收", "账龄"]].reset_index(drop=True),
        {"amount_col": "应收", "age_col": "账龄", "group_col": "客户"},
        df[["客户"]].to_dict("records"), ["客户"])
    p = writer.write_skill_results(
        [{"sheet_name": s, "df": d, "note": skills.SKILLS["ar_aging"]["note"]}],
        stem="note_top", staging=True)
    ws = openpyxl.load_workbook(p)[s]
    # 口径说明必须落在表顶第 1 行(财务据此核对口径)
    assert "口径" in str(ws.cell(1, 1).value)


# ── 优化循环第14轮(补货:摄取边界)两级表头不再选错表头行 ──

def test_two_level_merged_header_picks_real_header_row(tmp_path):
    import importlib, openpyxl
    app_mod = importlib.import_module("client.webui.app")
    # 第1行是跨列合并的年份(合并后只剩2个非空),第2行才是真表头。
    # 旧实现「前10行第一个含≥2字符串的行」会选中年份行 → 列名变 Unnamed:N、真表头沦为数据行。
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append([None, "2024年", None, "2025年", None])
    ws.append(["大区", "收入", "成本", "收入", "成本"])
    ws.append(["华东", 100, 60, 120, 70])
    ws.merge_cells("B1:C1"); ws.merge_cells("D1:E1")
    p = tmp_path / "twolevel.xlsx"; wb.save(p)

    df, sheet, header_row, dropped = app_mod._smart_read(p, ".xlsx")
    cols = [str(c) for c in df.columns]
    assert header_row == 1, f"应选第2行作表头,实际 header_row={header_row}"
    assert not any(c.startswith("Unnamed:") for c in cols), f"仍有 Unnamed 列: {cols}"
    assert "大区" in cols and "收入" in cols
    # 真表头行不能再作为数据行留在表里
    assert "大区" not in df.iloc[:, 0].astype(str).tolist()


def test_single_title_row_still_detected(tmp_path):
    import importlib, openpyxl
    app_mod = importlib.import_module("client.webui.app")
    # 单个大标题行(只有1个非空)的老行为不能被破坏
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["2024年度报表", None, None])
    ws.append(["大区", "金额", "成本"])
    ws.append(["华东", 100, 60])
    p = tmp_path / "title.xlsx"; wb.save(p)
    df, sheet, header_row, dropped = app_mod._smart_read(p, ".xlsx")
    assert [str(c) for c in df.columns] == ["大区", "金额", "成本"]


# ── 优化循环第15轮(补货:定时任务)每月31号=月末,不再一律夹到28号 ──

def _monthly(dom):
    from client.webui.scheduler import ScheduledTask
    return ScheduledTask(id="x", username="u", name="月结", question="q",
                         schedule_kind="monthly", day_of_month=dom, at_hour=9, at_minute=0)


def test_monthly_month_end_lands_on_real_last_day():
    from datetime import datetime
    t = _monthly(31)
    # 31 天的月份必须真的落在 31 号(旧实现一律 28 号 → 月结报表赶不上月末)
    assert t.compute_next_run(datetime(2026, 1, 5)).date().isoformat() == "2026-01-31"
    assert t.compute_next_run(datetime(2026, 3, 5)).date().isoformat() == "2026-03-31"
    # 2 月自动落到当月最后一天;闰年是 29 号
    assert t.compute_next_run(datetime(2026, 2, 5)).date().isoformat() == "2026-02-28"
    assert t.compute_next_run(datetime(2028, 2, 5)).date().isoformat() == "2028-02-29"


def test_monthly_mid_month_unchanged():
    from datetime import datetime
    t = _monthly(15)
    for base, want in ((datetime(2026, 1, 5), "2026-01-15"),
                       (datetime(2026, 2, 5), "2026-02-15")):
        assert t.compute_next_run(base).date().isoformat() == want


def test_monthly_rolls_over_year():
    from datetime import datetime
    t = _monthly(31)
    assert t.compute_next_run(datetime(2026, 12, 31, 9, 30)).date().isoformat() == "2027-01-31"


# ── 优化循环第16轮(补货:审计链)崩溃截断不再误报篡改/吞事件 ──

def _audit_tmp(tmp_path, monkeypatch):
    from client.he_ops import audit
    monkeypatch.setattr(audit, "_path", lambda u: tmp_path / f"{u}.jsonl")
    audit._last_hash.clear()
    return audit


def test_audit_crash_truncated_tail_not_reported_as_tamper(tmp_path, monkeypatch):
    audit = _audit_tmp(tmp_path, monkeypatch)
    for i in range(3):
        audit._append("u", {"type": "t", "i": i})
    p = tmp_path / "u.jsonl"
    p.write_text(p.read_text(encoding="utf-8")[:-15], encoding="utf-8")   # 模拟写入时崩溃
    r = audit.verify_chain("u")
    # 末行残缺是崩溃残留,不能与"疑似篡改"混为一谈
    assert r.get("truncated_tail") is True
    assert "崩溃" in r["reason"] and "篡改" not in r["reason"].split("非篡改")[0]


def test_audit_append_after_crash_does_not_swallow_event(tmp_path, monkeypatch):
    audit = _audit_tmp(tmp_path, monkeypatch)
    for i in range(3):
        audit._append("u", {"type": "t", "i": i})
    p = tmp_path / "u.jsonl"
    p.write_text(p.read_text(encoding="utf-8")[:-15], encoding="utf-8")
    before = len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()])
    audit._last_hash.clear()
    audit._append("u", {"type": "t", "i": "after-crash"})
    after = len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()])
    # 残行没有换行符,旧实现会把新事件粘上去 → 行数不变=事件被吞
    assert after == before + 1


def test_audit_still_detects_real_tampering(tmp_path, monkeypatch):
    import json
    audit = _audit_tmp(tmp_path, monkeypatch)
    for i in range(3):
        audit._append("u", {"type": "t", "i": i})
    p = tmp_path / "u.jsonl"
    ls = p.read_text(encoding="utf-8").splitlines()
    ev = json.loads(ls[1]); ev["i"] = 999                 # 改中间行字段
    ls[1] = json.dumps(ev, ensure_ascii=False)
    p.write_text("\n".join(ls) + "\n", encoding="utf-8")
    r = audit.verify_chain("u")
    assert r["ok"] is False and "hash 不匹配" in r["reason"]


def test_audit_chain_survives_concurrent_appends(tmp_path, monkeypatch):
    import threading
    audit = _audit_tmp(tmp_path, monkeypatch)

    def w(n):
        for i in range(20):
            audit._append("u", {"type": "c", "t": n, "i": i})
    ths = [threading.Thread(target=w, args=(k,)) for k in range(5)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    r = audit.verify_chain("u")
    assert r["ok"] is True and r["total"] == 100
