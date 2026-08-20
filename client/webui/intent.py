"""用户请求意图识别；保持纯函数，避免和执行流水线耦合。"""

from __future__ import annotations

import re
from typing import Optional


ANALYSIS_KEYWORDS = (
    "统计", "计算", "算一下", "算下", "算出", "算算", "分析", "汇总", "排名", "排行",
    "明细", "对比", "占比", "完成率", "回款率", "毛利率", "比率", "比例", "平均", "均值",
    "总和", "求和", "总计", "合计", "按", "分组", "分布", "分类", "描述", "概览", "概述",
    "趋势", "预测", "环比", "同比", "看每", "看各", "每位", "每个", "每人", "每月", "每天",
    "Excel", "表格", "导出", "出表", "sum ", "mean ", "average", "count(", "group by",
    "groupby", "analyze", "analyse", "stats", "top", "bottom", "rank",
)
KNOWLEDGE_Q_MARKERS = (
    "是什么", "什么是", "啥是", "是啥", "什么意思", "啥意思", "何为", "怎么算", "怎样算",
    "如何算", "怎么计算", "怎样计算", "如何计算", "计算口径", "计算方式", "计算方法", "计算公式",
    "的公式", "公式是", "定义", "含义", "概念", "原理", "区别", "介绍一下", "介绍下",
    "解释一下", "解释下", "科普", "为什么", "怎么理解", "适用于什么", "什么场景", "有哪些方法",
    "怎么做", "如何做",
)
DATA_OP_MARKERS = (
    "这份", "这个表", "这张表", "这些数据", "表里", "数据中", "数据里", "查询结果", "数据库结果",
    "上传", "附件", "文件里", "文件中", "每个人", "每位", "每人", "各位", "top", "排名",
    "排行", "导出", "出表", "生成excel", "生成 excel",
)
WEB_LOOKUP_MARKERS = (
    "天气", "气温", "下雨", "下雪", "降雨", "台风", "空气质量", "雾霾", "新闻", "最新消息",
    "热点", "头条", "实时", "股价", "股市", "大盘", "指数", "汇率", "油价", "金价", "票价",
    "机票", "比分", "赛事", "赛程", "票房", "上映", "几点开", "现在几点", "今天几号", "今天是",
    "明天", "后天", "近期", "搜索", "查询", "查找", "查一下", "搜一下", "查查", "搜搜", "百度",
    "谷歌", "google", "上网查", "联网",
)
SCHEDULE_MARKERS = (
    "每天", "每日", "每周", "每星期", "每月", "每个月", "每隔", "每小时", "工作日", "周末",
    "双休", "定时", "定期", "自动跑", "自动运行", "按时", "每分钟", "每天早上", "每天晚上",
    "每周一", "每周五",
)
TASK_INTENT_MARKERS = ("定时任务", "创建任务", "建个任务", "设个任务", "设定任务", "schedule", "cron")


def looks_like_knowledge_question(user_query: str) -> bool:
    if not user_query:
        return False
    query = user_query.lower()
    if any(marker.lower() in query for marker in DATA_OP_MARKERS):
        return False
    return any(marker.lower() in query for marker in KNOWLEDGE_Q_MARKERS)


def looks_like_web_lookup(user_query: str) -> bool:
    if not user_query:
        return False
    query = user_query.lower()
    if any(marker.lower() in query for marker in DATA_OP_MARKERS):
        return False
    return any(marker.lower() in query for marker in WEB_LOOKUP_MARKERS)


def looks_like_no_intent(user_query: str) -> bool:
    query = (user_query or "").strip()
    if not query:
        return True
    if re.search(r"[一-鿿]", query):
        return False
    word_like = re.findall(r"[A-Za-z0-9]{2,}", query)
    non_space = re.sub(r"\s", "", query)
    alnum_ratio = len(re.sub(r"[^A-Za-z0-9]", "", query)) / max(len(non_space), 1)
    return not word_like or alnum_ratio < 0.3


def looks_like_schedule_request(user_query: str) -> bool:
    if not user_query:
        return False
    query = user_query.strip()
    if any(marker in query for marker in TASK_INTENT_MARKERS):
        return True
    if looks_like_knowledge_question(query) or not any(marker in query for marker in SCHEDULE_MARKERS):
        return False
    has_clock = bool(re.search(r"\d{1,2}\s*[点:：]", query)) or any(
        word in query for word in ("早上", "早晨", "上午", "中午", "下午", "晚上", "傍晚", "凌晨", "夜里")
    )
    strong = (
        any(word in query for word in ("工作日", "周末", "双休", "每周", "每星期", "每月", "每个月", "每隔", "每小时", "每分钟"))
        or bool(re.search(r"(?:周|星期)[一二三四五六日天]", query))
        or bool(re.search(r"\d{1,2}\s*[号日]", query))
    )
    return has_clock or strong


def detect_intent_ambiguity(user_query: str, has_attachment: bool) -> Optional[dict]:
    if has_attachment and user_query and looks_like_schedule_request(user_query):
        return {
            "kind": "schedule_vs_oneshot",
            "question": "你的描述里既有「定时」的意思,又带了一个附件 —— 这两种做法不一样,你想要哪种?",
            "options": [
                {"label": "创建定时任务,按计划自动处理(附件只作示例 / 之后按文件夹取最新)", "action": "wizard"},
                {"label": "只分析当前这个附件一次(忽略「定时」)", "action": "analyze"},
            ],
            "allow_free": True,
        }
    return None


def looks_like_analysis(user_query: str) -> bool:
    if not user_query or looks_like_knowledge_question(user_query) or looks_like_web_lookup(user_query):
        return False
    query = user_query.lower()
    return any(keyword.lower() in query for keyword in ANALYSIS_KEYWORDS) or len(user_query) > 60
