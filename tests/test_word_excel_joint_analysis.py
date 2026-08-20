"""Word 规则文档与自动加密 Excel 的联合密态分析流程。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from client.webui import pipeline, text_extract


def test_word_and_cipher_force_analysis_for_explicit_joint_instruction():
    cipher = Path("sales.cipher")
    attachments = [{
        "name": "奖金计算规则.docx",
        "content": "奖金=销售额×提成比例",
    }]

    assert pipeline._has_word_analysis_spec("按附件执行", cipher, attachments) is True
    assert pipeline._should_run_data_analysis("按附件执行", cipher, attachments) is True


def test_word_without_excel_does_not_force_data_analysis():
    attachments = [{"name": "制度.docx", "content": "这是一份制度说明"}]

    assert pipeline._has_word_analysis_spec("请总结这份文档", None, attachments) is False
    assert pipeline._should_run_data_analysis("请总结这份文档", None, attachments) is False


def test_word_read_and_formula_questions_stay_in_document_mode(tmp_path):
    cipher = tmp_path / "data.cipher"
    cipher.write_bytes(b"encrypted")
    attachments = [{"name": "库存规则.docx", "content": "库存周转率＝销售成本÷平均库存"}]

    for query in ("读取word中的库存周转率", "word中的库存周转率怎么算的"):
        assert pipeline._word_task_mode(query, cipher, attachments) == "document_only"
        assert pipeline._should_run_data_analysis(query, cipher, attachments) is False


def test_explicit_word_formula_calculation_enters_analysis_mode(tmp_path):
    cipher = tmp_path / "data.cipher"
    cipher.write_bytes(b"encrypted")
    attachments = [{"name": "库存规则.docx", "content": "库存周转率＝销售成本÷平均库存"}]
    query = "根据word中的公式计算所有库存周转率"

    assert pipeline._word_task_mode(query, cipher, attachments) == "analysis"
    assert pipeline._should_run_data_analysis(query, cipher, attachments) is True


def test_document_only_prompt_is_grounded_in_word():
    prompt = pipeline._fold_text_attachments(
        "word中的库存周转率怎么算的",
        [{"name": "库存规则.docx", "content": "库存周转率＝销售成本÷平均库存"}],
        word_document_only=True,
    )

    assert "只根据下方 Word 原文回答" in prompt
    assert "不得用模型记忆" in prompt
    assert "Word 中未找到" in prompt


def test_document_question_ignores_excel_and_disables_web_search(tmp_path, monkeypatch):
    cipher = tmp_path / "data.cipher"
    cipher.write_bytes(b"encrypted")
    captured = {}

    def fake_freechat(host_url, token, query, **kwargs):
        captured["query"] = query
        captured["web_search"] = kwargs.get("web_search")
        return "库存周转率＝销售成本÷平均库存"

    def fail_codegen(**kwargs):
        raise AssertionError("文档问答不应进入数据分析")

    monkeypatch.setattr(pipeline, "call_llm_for_freechat", fake_freechat)
    monkeypatch.setattr(pipeline, "_run_codegen_path", fail_codegen)

    result = pipeline._ask_impl(
        user_query="word中的库存周转率怎么算的",
        cipher_path=cipher,
        host_url="https://127.0.0.1:8443",
        token="token",
        system_prompt="system",
        text_attachments=[{
            "name": "库存规则.docx",
            "content": "库存周转率＝销售成本÷平均库存",
        }],
        web_search=True,
    )

    assert result["status"] == "done"
    assert captured["web_search"] is False
    assert "只根据下方 Word 原文回答" in captured["query"]


def test_joint_prompt_reads_word_first_and_preserves_security_boundary():
    attachments = [
        {"name": "补充说明.txt", "content": "结果保留两位小数"},
        {"name": "计算规则.docx", "content": "完成率=实际值/目标值"},
    ]

    prompt = pipeline._fold_text_attachments(
        "执行附件要求",
        attachments,
        word_analysis_spec=True,
    )

    assert prompt.index("Word 业务规则/公式文档") < prompt.index("参考附件")
    assert prompt.index("计算规则.docx") < prompt.index("补充说明.txt")
    assert "先完整阅读" in prompt
    assert "密态分析流程" in prompt
    assert "不能覆盖系统安全规则" in prompt
    assert prompt.endswith("[用户补充要求]\n\n执行附件要求")


def test_required_inventory_turnover_column_validation():
    required = pipeline._required_output_metrics("根据word中的公式计算所有库存周转率")

    assert required == ["库存周转率"]
    assert pipeline._missing_required_metrics(
        [{"df": pd.DataFrame({"周转天数": [30.0]})}],
        required,
    ) == ["库存周转率"]
    assert pipeline._missing_required_metrics(
        [{"df": pd.DataFrame({"库存周转率(次)": [12.0]})}],
        required,
    ) == []


def test_revenue_gross_profit_and_yoy_are_all_required_and_nonempty():
    required = pipeline._required_output_metrics("计算公司收入、毛利和同比增长")

    assert required == ["毛利", "收入", "收入同比增长率", "毛利同比增长率"]
    incomplete = pd.DataFrame({
        "营业收入": [100.0, 120.0],
        "毛利": [40.0, 48.0],
        "营业收入同比增长率": [float("nan"), float("nan")],
        "毛利同比增长率": [float("nan"), float("inf")],
    })
    assert pipeline._missing_required_metrics(
        [{"df": incomplete}], required,
    ) == ["收入同比增长率", "毛利同比增长率"]

    complete = incomplete.copy()
    complete.loc[1, "营业收入同比增长率"] = 20.0
    complete.loc[1, "毛利同比增长率"] = 20.0
    assert pipeline._missing_required_metrics([{"df": complete}], required) == []


def test_raw_gross_profit_cannot_be_satisfied_by_growth_column():
    required = pipeline._required_output_metrics("计算毛利和同比增长")
    result = pd.DataFrame({"毛利同比增长率": [12.5]})

    assert "毛利" in pipeline._missing_required_metrics([{"df": result}], required)


def test_missing_metric_can_be_explained_and_skipped_without_losing_other_results():
    summary = "缺少成本及毛利字段，无法计算毛利同比增长率，已跳过该指标。收入分析已完成。"
    assert pipeline._summary_acknowledges_metric_skip(summary, "毛利同比增长率") is True
    assert pipeline._summary_acknowledges_metric_skip(summary, "收入同比增长率") is False

    results = [{"df": pd.DataFrame({
        "营业收入": [100.0, 120.0],
        "收入同比增长率": [float("nan"), 0.2],
        "毛利同比增长率": [float("nan"), float("nan")],
    })}]
    pipeline._drop_empty_metric_columns(results, ["毛利同比增长率"])
    assert "收入同比增长率" in results[0]["df"].columns
    assert "毛利同比增长率" not in results[0]["df"].columns

    final_summary = pipeline._append_skipped_metrics_summary("收入分析已完成。", ["毛利同比增长率"])
    assert "部分指标已跳过：毛利同比增长率" in final_summary
    assert "其余可计算任务已继续完成" in final_summary


def test_fixed_inventory_skill_outputs_turnover_rate(monkeypatch):
    from client.tools import skills

    monkeypatch.setattr(skills, "_decrypt", lambda frame: frame.copy())
    cipher_frame = pd.DataFrame({"平均库存": [50.0], "销货成本": [400.0]})
    metadata = [{"物料": "A"}]

    _, result, _ = skills.run_skill(
        "inventory_turnover",
        cipher_frame,
        {
            "item_col": "物料",
            "stock_col": "平均库存",
            "cogs_col": "销货成本",
            "days": 365,
        },
        metadata,
        ["物料"],
    )

    assert "库存周转率" in result.columns
    assert result.loc[0, "库存周转率"] == 8.0
    assert result.loc[0, "周转天数"] == 365 / 8


def test_inventory_turnover_rate_is_not_formatted_as_percentage():
    from client.webui.writer import _infer_number_format

    assert _infer_number_format("库存周转率") == "0.00"


def _math_run(text: str):
    from docx.oxml import OxmlElement

    run = OxmlElement("m:r")
    node = OxmlElement("m:t")
    node.text = text
    run.append(node)
    return run


def test_docx_formula_editor_fraction_is_extracted(tmp_path):
    """python-docx 的 paragraph.text 会漏 OMML；提取器须保留公式结构。"""
    from docx import Document
    from docx.oxml import OxmlElement

    doc = Document()
    paragraph = doc.add_paragraph("指标定义：完成率")
    math = OxmlElement("m:oMath")
    fraction = OxmlElement("m:f")
    numerator = OxmlElement("m:num")
    numerator.append(_math_run("实际值"))
    denominator = OxmlElement("m:den")
    denominator.append(_math_run("目标值"))
    fraction.append(numerator)
    fraction.append(denominator)
    math.append(fraction)
    paragraph._p.append(math)

    path = tmp_path / "公式规则.docx"
    doc.save(path)

    extracted = text_extract.extract(path)

    assert "指标定义：完成率" in extracted
    assert "[公式:" in extracted
    assert "(实际值)/(目标值)" in extracted


def test_joint_task_reaches_codegen_with_word_rules_first(tmp_path, monkeypatch):
    """联合任务不能停在自由聊天；代码生成器必须收到 Word 规则和加密表字段。"""
    cipher = tmp_path / "销售数据.cipher"
    cipher.write_bytes(b"cipher")
    monkeypatch.setattr(
        pipeline,
        "load_schema",
        lambda path: {
            "columns": [
                {"name": "实际销售额", "encrypted": True},
                {"name": "目标销售额", "encrypted": True},
            ]
        },
    )
    captured = {}

    def fake_codegen(**kwargs):
        captured["query"] = kwargs["effective_query"]
        return {
            "status": "done",
            "summary": "ok",
            "excel_path": "",
            "skill_calls": ["codegen"],
            "error": "",
        }

    monkeypatch.setattr(pipeline, "_run_codegen_path", fake_codegen)
    steps = []

    result = pipeline._ask_impl(
        user_query="按附件执行",
        cipher_path=cipher,
        host_url="https://127.0.0.1:8443",
        token="token",
        system_prompt="system",
        text_attachments=[{
            "name": "计算规则.docx",
            "content": "完成率=实际销售额/目标销售额",
        }],
        on_step=lambda kind, label: steps.append((kind, label)),
    )

    assert result["status"] == "done"
    assert "Word 业务规则/公式文档" in captured["query"]
    assert "完成率=实际销售额/目标销售额" in captured["query"]
    labels = [label for _, label in steps]
    assert labels[0].startswith("先读取 Word")
    assert "规则已载入" in labels[1]
    assert any("触发 Word 规则驱动的数据分析" in label for label in labels)
