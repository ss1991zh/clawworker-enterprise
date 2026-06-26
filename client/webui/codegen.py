"""
代码生成 + 安全执行引擎(SKILL.md 架构)。

流程:
  ① build_codegen_messages —— 把相关 SKILL.md 教学内容 + schema + 问题组装成 LLM 提示
  ② extract_code           —— 从 LLM 回复里抠 ```python``` 代码块
  ③ ast_safety_check       —— AST 扫描,拒危险调用 / import / dunder 逃逸
  ④ run_generated_code     —— 受限 exec:
        - 只暴露 ps / ct / hp / hl / pd / np + cdf + metadata + results
        - 自定义 __import__,把 crypto_toolkit 换成"解密门控代理"
        - 首次 ct.decrypt* → 触发解密授权(prompt_decrypt)
        - 代码把结果写进 results = [{sheet_name, df, chart}]

安全模型:同进程受限 exec。威胁是"LLM 写错/越界代码",不是恶意用户
(密钥本就在本机)。受限 builtins + import 白名单 + AST 扫描 + dunder 拦截。
"""

from __future__ import annotations

import ast
import re
from typing import Any, Callable, Optional

from client.he_ops import synth as _synth_mod
from client.he_ops import groupby as _groupby_mod
from client.he_ops import window as _window_mod


def _to_ca(x):
    """pandaseal CipherSeries → henumpy CipherArray(其余原样)。
    让生成代码能直接把 `cdf[列]` 喂给 synth/groupby/window。"""
    return x.to_cipherarray() if type(x).__name__ == "CipherSeries" else x


class _BoundHp:
    """把模块函数的第一个 hp 参数预绑定,并把 CipherSeries 实参自动转 CipherArray,
    生成代码里直接 `synth.sumif_gt(cdf[列], 阈值)` / `window.rolling_mean(cdf[列], 3)`。"""

    def __init__(self, hp, mod):
        self._hp = hp
        self._mod = mod

    def __getattr__(self, name):
        fn = getattr(self._mod, name)
        hp = self._hp
        return lambda *a, **k: fn(hp, *[_to_ca(x) for x in a],
                                  **{kk: _to_ca(vv) for kk, vv in k.items()})


# 兼容旧名:synth 绑定
def _BoundSynth(hp):
    return _BoundHp(hp, _synth_mod)


class _BoundGroupby:
    """密态分组聚合(明文键 × 密文度量):
      groupby.sum(cdf[度量], keys) / .mean / .count(keys) / .max / .min / .agg(度量, keys, "sum")
    keys 取自明文身份列(metadata_rows 的某列)。sum/mean/count 精确,max/min 近似。"""

    def __init__(self, hp):
        self._hp = hp

    def sum(self, measure, keys):
        return _groupby_mod.groupby_sum(self._hp, _to_ca(measure), keys)

    def mean(self, measure, keys):
        return _groupby_mod.groupby_mean(self._hp, _to_ca(measure), keys)

    def count(self, keys):
        return _groupby_mod.groupby_count(keys)

    def max(self, measure, keys):
        return _groupby_mod.groupby_max(self._hp, _to_ca(measure), keys)

    def min(self, measure, keys):
        return _groupby_mod.groupby_min(self._hp, _to_ca(measure), keys)

    def agg(self, measure, keys, agg="sum"):
        if agg == "count":
            return _groupby_mod.groupby_count(keys)
        return _groupby_mod.groupby_agg(self._hp, _to_ca(measure), keys, agg)


# ---------------------------------------------------------------------------
# 信号异常
# ---------------------------------------------------------------------------

class CodegenCancelled(Exception):
    """用户在解密授权门点了取消 / 停止。"""


class KeepEncrypted(Exception):
    """用户在解密授权门选了「保留密文」—— 不解密,导出源密文。"""


class DecryptionFailed(Exception):
    """
    用户已授权解密,但真实 ct.decrypt* 报错(密钥/密文不匹配、维度不符、
    密文损坏等)。这是**终态错误**,不是「代码写错了」—— 不应回退固化 skill
    (固化路径用同一套密钥/密文,只会再失败一次),应直接把原因报给用户。
    """


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

  ct 加解密**只有这 4 个方法**(别臆造 encrypt_ndarray / decrypt_array 之类):
    · ct.encrypt(x)      —— 加密数值 / list / numpy 数组(ndarray 直接传)
    · ct.encrypt_df(df)  —— 加密 pandas DataFrame
    · ct.decrypt(c)      —— 解密 ct.encrypt 出来的数值密文 → ndarray
    · ct.decrypt_df(c)   —— 解密 CipherDataFrame / CipherSeries → 明文 DataFrame

═══════════ 在密文上直接计算(进阶,仅"保留密文/不解密"场景才用) ═══════════
默认仍按下面的最稳写法:**先 `ct.decrypt_df(cdf)` 再用 pandas/numpy**(本机内存、有解密授权门控)。
只有需要"保留密文、不解密就出结果"时,才在密文上用 henumpy(hp)算子:
  实测可靠:sum/mean/var/std/median/percentile/max/min/div/sort/sqrt/exp/log 等,直接 hp.xxx(密文)。
  ⚠ 当前构建里 `hp.greater / greater_equal / less / digitize` **不可靠,禁止使用**。
    要"比较 / 条件求和 / 分箱"时,改用已就绪的 `synth` 工具(无需 import;阈值用明文数字;
    可直接把 `cdf[列]` 当密文列传,会自动转 CipherArray):
      · synth.gt(a, b) / synth.lt(a, b)          两个密文逐元素比较 → 1/0 掩码
      · synth.sumif_gt(col, 阈值)                 条件求和(col 中 > 阈值 的元素之和,SUMIF)
      · synth.countif_gt(col, 阈值)               条件计数(COUNTIF)
      · synth.bin_index(col, [阈值1, 阈值2, ...])  分箱序号(替代 digitize;RFM/ABC 分级用)
      · 多条件(AND/OR):mask 用布尔代数组合再聚合 ——
        synth.sumif_and(col, [synth.gt_threshold(col,a), synth.lt_threshold(col,b)])  # a<col<b 求和
        synth.sumif_or / countif_and / countif_or 同理;synth.band/bor/bnot 组合任意掩码。
      · 排名/top-k(比较和,替代近似 sort;n=行数):synth.topk_sum(col, k, n) / topk_mean / bottomk_sum
        —— 隐私友好:只出"最大/最小 k 个之和",不暴露是哪几个、不暴露顺序。要逐行排名用 synth.rank(col, n)。
  密态分组聚合 `groupby`(明文维度键 × 密文度量,sum/mean/count 精确、max/min 近似):
      keys = [r["大区"] for r in metadata_rows]                       # 维度键取自明文身份列
      g = groupby.sum(cdf["销售额"], keys)   # → {大区: 密文标量}
      合计 = {k: float(ct.decrypt(v)) for k, v in g.items()}          # 标量密文解密:用 float(ct.decrypt(v)),勿写 [0]
      groupby.mean / groupby.count(keys) / groupby.max / groupby.min / groupby.agg(col, keys, "sum")
  窗口/时序 `window`(diff/lag/rolling 精确,pct_change 近似):
      window.diff(col, 1)  环比差额  · window.rolling_mean(col, 3)  移动平均
      window.pct_change(col, 1)  变化率(近似)  · window.lag(col, 1)  上一期
  密态模型 `hl`(对拍达标、可用):LinearRegression(回归)、LogisticRegression(分类,
    predict 返回 logit,取标签用 `logit>0`,特征须已标准化)。⚠ GBDT/XGBoost 当前构建训练报错,勿用。

═══════════ 第 0 步:意图补全(先想清楚"要呈现什么",再写代码) ═══════════
用户的话经常只说"算什么",省略"怎么呈现"。你必须先从【问题 + schema】推断**完整意图**,
默认补齐下面这些(除非用户已明确指定别的):
  · 排序     —— 凡涉及排名 / 达成 / 比率 / 金额对比 → 按关键指标 sort_values(降序),
                并加一列「排名」= range(1, n+1)。
  · 配图     —— 凡有可对比维度 → 必配 chart。趋势/预测→line;构成/对比→bar;占比→bar。
  · 图表可看性(站在"人看 Excel"的角度想)——一张图塞太多类别就没法看:
      ① 类别 > ~20 个(如 100 个人)→ 别挤一张图,chart 加 `split_by="<上一层维度>"`
         (如按"销售大区"拆),渲染端会按该列每组各出一张图、都带完整标题+横纵轴;
         表格也会先按该维度排序(先大区、组内再按指标降序)。
      ② 用户点名**多个实体**(如 DR-400 和 SM-200 两个产品的预测)→ 结果含一列区分实体,
         chart 用 `split_by="<实体列>"` → 每个实体各出一张图(两产品=两张趋势图)。
      ③ 也可给 `charts:[多个 chart 规格]` 一张表配多张不同的图。
      ④ 退一步:用 TOP-N 只画前 N,别无脑全画。
  · 历史背景 —— 预测/趋势类**绝不能只给未来值**:把「历史实际 + 未来预测」放进同一张表,
                历史值/预测值各一列(另一列留空 None),用折线连起来(有维度则多条线)。
  · 派生指标 —— 只给原始值不够,按业务补:达成率 / 同比环比 / 占比 / 差额 / 人均 等。
  · 档位标签 —— 给关键结果加一列**文字档位**(达成/未达成、超支/节约、A/B/C、正常/预警/异常、
                重要价值/待挽留…),渲染端会自动按语义上色。

═══════════ 场景 → 呈现配方(按数据领域套用) ═══════════
  销售:完成率/回款率排名(降序+排名列+柱状+达成档位)、区域对比柱状、趋势预测(历史+预测折线)。
  财务:预算vs实际(差额+差异率+超支/节约标签)、同比环比、杜邦/财务比率体系。
  库存:周转天数、ABC 分类(A/B/C 文字档位)、呆滞标记、缺货预警。
  HR  :人数构成与占比、绩效分级(优/良/中/差档位)、薪酬分布、人效对比。
  客户:RFM 分群(分群文字档位)、留存曲线(折线)、LTV 排名、流失名单。
  通用口诀:能排序就排序,能配图就配图,能加占比/合计就加,关键列给文字档位。

═══════════ 你的代码必须遵守 ═══════════
1. 不要写 import 语句,不要写初始化(环境已就绪)。
2. 把最终**已解密**的结果表写进 `results` 列表。每个元素是一个 dict,
   除 sheet_name/df 外的键**全部可选,声明即美化**(渲染端统一做表头高亮/冻结/
   自动筛选/隔行底色/百分比色阶/列宽自适应,你**不要**自己写样式):
     {
       "sheet_name": "中文表名",
       "df": <pandas.DataFrame 明文>,
       "chart": {"type":"bar"|"line", "x":"列名", "y":"列名"|["列1","列2"], "title":"标题",
                 "split_by":"<可选·按此列每组拆一张图,治"100人挤一图">"},
       "charts": [ {…}, {…} ],             # 可选:一张表配多张图

       "tier_col": "<档位文字列名,如 达成情况 / ABC分类 / 客户分群>",  # 该列按语义上色
       "total_row": True,                  # 末尾自动加「合计」行(金额求和/百分比取均值)
       "note": "<表顶一行说明,如 实线为历史、后6期为预测>",
       "number_formats": {"列名":"0.00%"}  # 仅在列名推断不准时才显式覆盖
     }
3. 解密**只认 ct.decrypt_df**:对 cdf / 任何 CipherDataFrame / CipherSeries
   (cdf 的列、`cdf['a']+cdf['b']`、密态聚合等结果)一律 `ct.decrypt_df(...)` 取明文;
   `ct.decrypt(...)` **仅**用于 `ct.encrypt(...)` 出来的数值数组——把 CipherSeries 喂给
   `ct.decrypt` 会报 `Unable to decrypt data of CipherSeries type`。
   **最稳写法:开头就 `df = ct.decrypt_df(cdf)`,之后全程用 pandas/numpy 处理。**
   解密在本机内存自动放行(计算完成后系统才向用户申请"解密展示"授权),直接调用即可。
4. 身份列已自动拼好:`df = ct.decrypt_df(cdf)` 返回的就是**完整明文表**——
   身份列(姓名/大区/月份/物料名…)在前,数值列在后,可直接
   `df.groupby("销售大区")` / `df["销售月份"]`,**不要再手动 merge metadata_rows**
   (会重复列)。metadata_rows / metadata_columns 仅在你需要单独取身份列时备用。
5. 派生指标用 pandas/numpy 算(如 回款率 = 回款金额 / 实际销售额);比率列保留小数(渲染端转百分比)。
6. 列名严格用 schema 里的字段名(含括号单位);字段缺失就在 summary 说明缺什么,别硬编造数。
7. 禁止:文件读写(open)、os/sys/subprocess/socket、网络、eval/exec、访问 __ 开头属性。
8. **全量处理铁律(每一行都是独立记录,永不合并行)**:
   输出主表一律**逐行明细**:行数 = 数据行数,可排序、可加排名/档位/派生列。
   "按大区 / 各产品线 / 汇总"指的是**排序方式**(先按该维度排、组内再按指标降序),
   **不是 groupby 把行压缩合并**;需要聚合视角时,只能在逐行明细之外**另加**聚合 sheet。
   禁止 head() / sample() / iloc 截断;只有用户明确要 TOP/前 N 或筛选
   (如"找出异常/超期")才允许输出子集。数据多≠只算一部分。
9. **数值稳健(避免崩溃)**:任何拟合 / 回归 / 相关性 / 分位数前,先清洗——
   `s = s[np.isfinite(s)]` 去掉 NaN/inf;除法防 0(分母 `.replace(0, np.nan)` 或先判);
   `np.polyfit`/`lstsq`/`corr` 的输入**绝不能含 NaN**(否则报 "SVD did not converge")。
   样本不足(<3 点)或数据为常数(`np.ptp(x)==0`)时跳过拟合、只给已有结果并在 summary 说明。

═══════════ 趋势 / 预测 稳健写法(照抄此骨架,别自由发挥) ═══════════
预测要"历史实际 + 未来预测"放同一张表 + 折线。用下面这套**防弹**写法
(明文最小二乘,已清洗 NaN、守卫退化;字段名按 schema 替换):

  full = ct.decrypt_df(cdf)                                   # 完整表(身份列已拼)
  ts = full.groupby("<时间列>")["<数值列>"].sum().sort_index() # 历史时间序列
  hist = ts.values.astype(float)
  mask = np.isfinite(hist); ts = ts[mask]; hist = hist[mask]  # 去 NaN/inf —— 关键!
  n = len(hist)
  rows = [{"<时间列>": str(m), "历史值": float(v), "预测值": None, "类型":"历史"} for m, v in ts.items()]
  if n >= 3 and np.ptp(hist) > 0:                             # 够点且非常数才预测
      x = np.arange(n, dtype=float); k, b = np.polyfit(x, hist, 1)
      last = str(ts.index[-1])
      for i in range(1, 7):                                   # 未来 6 期(按需改)
          rows.append({"<时间列>": f"{last}+{i}", "历史值": None,
                       "预测值": float(k*(n-1+i)+b), "类型":"预测"})
  out = pd.DataFrame(rows)
  results = [{"sheet_name":"<x>预测", "df": out, "note":"前为历史、后为预测",
              "chart":{"type":"line","x":"<时间列>","y":["历史值","预测值"],"title":"趋势:历史+预测"}}]
  # 多个实体(如 DR-400 + SM-200 两个产品):对每个实体重复上面、给 out 加一列"<实体列>",
  # 然后 chart 加 "split_by":"<实体列>" → 每个产品各出一张趋势图(而不是挤在一张)。

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
    try:   # 注入实测自动同步的算子可靠性摘要(随对拍报告保持准确)
        from client.he_ops.planner import capability_brief
        system = system + "\n\n═══════════ 密态算子可靠性(对拍实测·自动同步)═══════════\n" + capability_brief()
    except Exception:
        pass
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

# 允许 import 的模块(以及别名)。
# 原则:只放**纯计算 / 无副作用**的库与 stdlib(无文件/网络/进程/exec 能力);
# 危险模块(os/sys/subprocess/socket/shutil/pathlib/io/importlib/pickle…)永不入列。
_ALLOWED_IMPORTS = {
    # HE 工具链 + 数据分析
    "henumpy", "pandaseal", "crypto_toolkit", "helearn", "hetorch",
    "pandas", "numpy",
    # 安全 stdlib(纯计算 / 时间 / 文本 / 容器算法)
    "math", "datetime", "time", "calendar", "re", "json",
    "statistics", "decimal", "fractions", "random", "string",
    "collections", "itertools", "functools", "operator", "bisect", "heapq",
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

def _attach_identity(plain_df, cipher_arg, original_cdf, meta_df):
    """
    整表解密时,把身份列(姓名/大区/月份等明文)拼回明文 DataFrame 前面。

    `ct.decrypt_df(cdf)` 只还原加密的数值列;身份列在加密时被剥离存进 metadata。
    解密整表(cipher_arg 就是原始 cdf)时自动拼回,使 `df = ct.decrypt_df(cdf)`
    直接得到"身份列 + 数值列"的完整表,LLM 可直接 groupby("销售月份") 而不必手动 merge。
    只对原始整表生效;子表 / 单列解密不动。行数不匹配或无新增列则原样返回。
    """
    if meta_df is None or original_cdf is None or plain_df is None:
        return plain_df
    if cipher_arg is not original_cdf:
        return plain_df
    try:
        import pandas as pd
        if not hasattr(plain_df, "columns") or len(meta_df) != len(plain_df):
            return plain_df
        add = [c for c in meta_df.columns if c not in plain_df.columns]
        if not add:
            return plain_df
        merged = pd.concat(
            [meta_df[add].reset_index(drop=True), plain_df.reset_index(drop=True)],
            axis=1,
        )
        # 源数据表尾自带的合计行(身份列几乎全空)不是真实记录 —— 进分析前剔除,
        # 否则人数 +1、总和翻倍,输出再加合计就成双重合计
        from client.tools.skills import drop_source_total_rows
        merged, _ = drop_source_total_rows(merged)
        return merged
    except Exception:
        return plain_df


def _gated_decrypt(real_ct, name, args, kwargs, original_cdf=None, meta_df=None):
    """
    调真实解密 + 对 pandaseal 类型自动纠偏 + 整表解密自动拼回身份列。

    LLM 常误用 `ct.decrypt(CipherSeries)` / `ct.decrypt(CipherDataFrame)` ——
    但 `ct.decrypt` 只接受 `ct.encrypt(...)` 出来的数值密文数组,喂 pandaseal
    类型会抛 "Unable to decrypt data of CipherSeries type"。这里把 pandaseal
    类型转成正确入参再解密,等价于"用户本意是解密这一列/这张表"。
    正确映射(已用真实密文验证):
      ct.decrypt(CipherSeries)      → ct.decrypt(cs.to_cipherarray())      → ndarray
      ct.decrypt(CipherDataFrame)   → ct.decrypt_df(cdf)                   → DataFrame(+身份列)
      ct.decrypt_df(CipherSeries)   → ct.decrypt_df(cs.to_cipherdataframe())→ DataFrame
      ct.decrypt_df(cdf)            → DataFrame + 自动拼回身份列
    """
    fn = getattr(real_ct, name)
    if args:
        first, rest = args[0], args[1:]
        tname = type(first).__name__
        if name == "decrypt":
            if tname == "CipherSeries" and hasattr(first, "to_cipherarray"):
                return fn(first.to_cipherarray(), *rest, **kwargs)
            if tname == "CipherDataFrame" and hasattr(real_ct, "decrypt_df"):
                return _attach_identity(real_ct.decrypt_df(first), first, original_cdf, meta_df)
        elif name == "decrypt_df":
            if tname == "CipherSeries" and hasattr(first, "to_cipherdataframe"):
                return real_ct.decrypt_df(first.to_cipherdataframe())
            return _attach_identity(fn(*args, **kwargs), first, original_cdf, meta_df)
    return fn(*args, **kwargs)


# LLM 常臆造的 ct 方法名 → 真实方法。真实 API 只有:
#   加密:ct.encrypt(数值/list/ndarray) · ct.encrypt_df(DataFrame)
#   解密:ct.decrypt(数值密文数组) · ct.decrypt_df(CipherDataFrame/CipherSeries)
# 不存在 encrypt_ndarray / encrypt_array / decrypt_ndarray 这些。
_CT_ALIASES = {
    "encrypt_ndarray": "encrypt", "encrypt_array": "encrypt",
    "encrypt_numpy": "encrypt", "encrypt_np": "encrypt", "encrypt_arr": "encrypt",
    "encrypt_list": "encrypt", "encrypt_vector": "encrypt", "encrypt_value": "encrypt",
    "decrypt_ndarray": "decrypt", "decrypt_array": "decrypt",
    "decrypt_numpy": "decrypt", "decrypt_np": "decrypt", "decrypt_arr": "decrypt",
    "decrypt_list": "decrypt", "decrypt_vector": "decrypt", "decrypt_value": "decrypt",
    "encrypt_dataframe": "encrypt_df", "decrypt_dataframe": "decrypt_df",
    "encrypt_series": "encrypt_df", "decrypt_series": "decrypt_df",
}

_GATED_DECRYPT_NAMES = ("decrypt", "decrypt_df", "decrypt_ndarray", "decrypt_csv")


# crypto_toolkit 解密门控代理 —— 首次 decrypt 触发解密授权
class _CtGate:
    def __init__(self, real_ct, on_first_decrypt: Callable[[], None],
                 original_cdf=None, meta_df=None):
        object.__setattr__(self, "_ct", real_ct)
        object.__setattr__(self, "_on_first", on_first_decrypt)
        object.__setattr__(self, "_authorized", False)
        # 整表解密时自动拼回身份列用
        object.__setattr__(self, "_orig_cdf", original_cdf)
        object.__setattr__(self, "_meta_df", meta_df)

    def __getattr__(self, name):
        real_ct = object.__getattribute__(self, "_ct")
        orig_cdf = object.__getattribute__(self, "_orig_cdf")
        meta_df = object.__getattribute__(self, "_meta_df")
        # 方法名纠偏:LLM 臆造 encrypt_ndarray / decrypt_array 等不存在的名字时,
        # 映射到真实 API(ct.encrypt 本就吃 ndarray)。仍找不到才真 AttributeError。
        canonical = name if hasattr(real_ct, name) else _CT_ALIASES.get(name, name)
        attr = getattr(real_ct, canonical)
        if canonical in _GATED_DECRYPT_NAMES:
            def wrapped(*a, **k):
                if not object.__getattribute__(self, "_authorized"):
                    object.__getattribute__(self, "_on_first")()  # 可能 raise(取消/保留密文)
                    object.__setattr__(self, "_authorized", True)
                # 已授权解密 —— 真实解密若报错,是「解密失败」终态,
                # 包成 DecryptionFailed,避免上层误判为代码 bug 去回退固化 skill
                try:
                    return _gated_decrypt(real_ct, canonical, a, k,
                                          original_cdf=orig_cdf, meta_df=meta_df)
                except (CodegenCancelled, KeepEncrypted):
                    raise
                except Exception as e:  # noqa: BLE001
                    raise DecryptionFailed(f"{type(e).__name__}: {e}") from e
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
    decrypt 首次调用触发解密授权。
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

    # 解密授权门控回调
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

    # 身份列(明文)→ DataFrame,供整表解密时自动拼回
    meta_df = None
    try:
        if metadata_rows and metadata_columns:
            _mdf = pd.DataFrame(metadata_rows)
            _keep = [c for c in metadata_columns if c in _mdf.columns]
            if _keep:
                meta_df = _mdf[_keep]
    except Exception:
        meta_df = None

    ct_gate = _CtGate(ct, _on_first_decrypt, original_cdf=cdf, meta_df=meta_df)

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
        "synth": _BoundHp(hp, _synth_mod),       # 比较/条件求和/分箱/多条件布尔(补构建缺陷)
        "groupby": _BoundGroupby(hp),            # 密态分组聚合(明文键×密文度量)
        "window": _BoundHp(hp, _window_mod),     # 窗口/时序(diff/lag/rolling/pct_change)
        "results": results,
    }

    compiled = compile(code, "<generated_skill_code>", "exec")
    exec(compiled, sandbox_globals)  # noqa: S102 —— 受限命名空间 + AST 扫描

    # 取回 results(代码可能重新赋值 results = [...])
    final = sandbox_globals.get("results", results)
    if not isinstance(final, list):
        raise ValueError("生成代码没有产出 results 列表")
    # 规整:sheet_name + df 必填,呈现键(chart/charts/tier_col/total_row/note/
    # number_formats)原样透传 —— 渲染端(writer)按声明美化,丢了就退化成裸表
    cleaned: list[dict] = []
    for r in final:
        if not isinstance(r, dict):
            continue
        df = r.get("df")
        if df is None:
            continue
        item = {
            "sheet_name": r.get("sheet_name") or "结果",
            "df": df,
            "chart": r.get("chart"),
        }
        for k in ("charts", "tier_col", "total_row", "note", "number_formats"):
            v = r.get(k)
            if v:
                item[k] = v
        cleaned.append(item)
    return cleaned
