"""
算子能力表(operator capability registry)。

每个算子声明它在同态加密下的"脾气",这是多步规划器(Planner)做可行性/成本判断的地图,
也是对拍框架(parity)要逐个验证的清单。

字段:
  id        算子标识(= LLM/规划器引用名)
  category  arithmetic / aggregation / stats / comparison / sort / binning / math / linalg
  kind      "exact"  ≈ 仅加减乘,误差仅来自密文噪声(CKKS 近似计算,误差极小)
            "approx" 多项式近似实现(除法/开方/exp/log/比较/sign/排序…),误差更大、深度更高
  cost      "low" | "medium" | "high"  —— 乘法深度代价的粗分级(high 可能触发 bootstrap,慢)
  needs_auth_decrypt  True=该算子(或其常见用法,如全排序取 TOP-N)建议中途授权解密再继续
  arity     需要几个"密文数组"操作数(标量阈值等用闭包固化,不计入)
  he(hp, *ciphers) -> cipher    在密文上怎么算(henumpy)
  ref(np, *plains) -> plain     明文参照(numpy),对拍用
  note      备注:常见业务用途 / 实现策略 / 注意事项
  tol_abs   验收的绝对误差上限(按 kind 给默认,可被对拍实测收紧/放宽)

注:henumpy/pandaseal 已内置非常丰富的算子(比较/排序/分箱/除法/分位数/ML 都有),
本表是"挑出业务分析常用的那批 + 标注其 HE 特性",而不是重新实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from client.he_ops import synth

TOL_EXACT = 1e-2     # 精确类:仅密文噪声,通常 < 1e-3,留一档余量
TOL_APPROX = 5e-2    # 近似类:多项式近似,误差更大


@dataclass
class Op:
    id: str
    category: str
    kind: str
    cost: str
    needs_auth_decrypt: bool
    arity: int
    he: Callable
    ref: Callable
    note: str = ""
    tol_abs: Optional[float] = None
    impl: str = "native"   # native=直接用 henumpy;synth=用可靠原语合成(见 synth.py)

    def tol(self) -> float:
        if self.tol_abs is not None:
            return self.tol_abs
        return TOL_EXACT if self.kind == "exact" else TOL_APPROX


# --- 闭包里固化的标量参数(对拍时明文/密文用同一组)---
_CLIP_LO, _CLIP_HI = 0.2, 0.8
_BINS = [0.25, 0.5, 0.75]
_PCTL = 75

REGISTRY: list[Op] = [
    # ---------- arithmetic(精确:加减乘)----------
    Op("add", "arithmetic", "exact", "low", False, 2,
       lambda hp, a, b: hp.add(a, b), lambda np, a, b: np.add(a, b),
       note="逐元素相加。组合各类业务指标的基础。"),
    Op("sub", "arithmetic", "exact", "low", False, 2,
       lambda hp, a, b: hp.sub(a, b), lambda np, a, b: np.subtract(a, b),
       note="逐元素相减。差额/同比环比的基础。"),
    Op("mul", "arithmetic", "exact", "low", False, 2,
       lambda hp, a, b: hp.mul(a, b), lambda np, a, b: np.multiply(a, b),
       note="逐元素相乘(消耗 1 层乘法深度)。"),
    Op("negative", "arithmetic", "exact", "low", False, 1,
       lambda hp, a: hp.negative(a), lambda np, a: np.negative(a)),
    Op("square", "arithmetic", "exact", "low", False, 1,
       lambda hp, a: hp.square(a), lambda np, a: np.square(a),
       note="平方(1 层乘法)。方差/二阶矩用。"),
    Op("absolute", "arithmetic", "approx", "medium", False, 1,
       lambda hp, a: hp.absolute(a), lambda np, a: np.absolute(a),
       note="绝对值(经 sign/平方近似)。"),

    # ---------- division(近似:无原生除法)----------
    Op("div", "arithmetic", "approx", "high", False, 2,
       lambda hp, a, b: hp.div(a, b), lambda np, a, b: np.divide(a, b),
       note="除法(牛顿迭代近似,深度高)。回款率/占比类口径。分母明文时优先乘倒数。"),
    Op("reciprocal", "arithmetic", "approx", "high", False, 1,
       lambda hp, a: hp.reciprocal(a), lambda np, a: np.reciprocal(a),
       note="取倒数(近似)。"),

    # ---------- aggregation(归约)----------
    Op("sum", "aggregation", "exact", "low", False, 1,
       lambda hp, a: hp.sum(a), lambda np, a: np.sum(a),
       note="求和。最常用归约。"),
    Op("mean", "aggregation", "exact", "low", False, 1,
       lambda hp, a: hp.mean(a), lambda np, a: np.mean(a),
       note="均值(和乘明文 1/n)。"),
    Op("prod", "aggregation", "exact", "medium", False, 1,
       lambda hp, a: hp.prod(a), lambda np, a: np.prod(a),
       note="连乘(乘法深度随长度增长)。"),
    Op("cumsum", "aggregation", "exact", "low", False, 1,
       lambda hp, a: hp.cumsum(a), lambda np, a: np.cumsum(a),
       note="累计求和。"),
    Op("max", "aggregation", "approx", "high", True, 1,
       lambda hp, a: hp.max(a), lambda np, a: np.max(a),
       note="最大值(基于比较,深度高)。TOP/封顶用;大数据量建议授权解密后取。"),
    Op("min", "aggregation", "approx", "high", True, 1,
       lambda hp, a: hp.min(a), lambda np, a: np.min(a),
       note="最小值(基于比较)。"),

    # ---------- stats ----------
    Op("var", "stats", "exact", "medium", False, 1,
       lambda hp, a: hp.var(a), lambda np, a: np.var(a),
       note="方差(E[x²]-E[x]²)。"),
    Op("std", "stats", "approx", "high", False, 1,
       lambda hp, a: hp.std(a), lambda np, a: np.std(a),
       note="标准差(方差再开方,sqrt 近似)。"),
    Op("median", "stats", "approx", "high", True, 1,
       lambda hp, a: hp.median(a), lambda np, a: np.median(a),
       note="中位数(依赖排序,深度高)。建议授权解密后算。"),
    Op("percentile", "stats", "approx", "high", True, 1,
       lambda hp, a: hp.percentile(a, _PCTL), lambda np, a: np.percentile(a, _PCTL),
       note=f"分位数(P{_PCTL},依赖排序)。"),

    # ---------- comparison(近似:多项式近似 sign)----------
    Op("greater", "comparison", "approx", "high", False, 2,
       lambda hp, a, b: hp.greater(a, b), lambda np, a, b: (np.greater(a, b)).astype(float),
       note="a>b 返回 1/0(近似)。阈值筛选/条件聚合的地基。"),
    Op("greater_equal", "comparison", "approx", "high", False, 2,
       lambda hp, a, b: hp.greater_equal(a, b), lambda np, a, b: (a >= b).astype(float)),
    Op("less", "comparison", "approx", "high", False, 2,
       lambda hp, a, b: hp.less(a, b), lambda np, a, b: (a < b).astype(float)),
    Op("sign", "comparison", "approx", "high", False, 1,
       lambda hp, a: hp.sign(a), lambda np, a: np.sign(a),
       note="符号函数(多项式近似)。比较/绝对值的底层。"),
    Op("maximum", "comparison", "approx", "high", False, 2,
       lambda hp, a, b: hp.maximum(a, b), lambda np, a, b: np.maximum(a, b),
       note="逐元素取较大者。"),
    Op("minimum", "comparison", "approx", "high", False, 2,
       lambda hp, a, b: hp.minimum(a, b), lambda np, a, b: np.minimum(a, b)),
    Op("clip", "comparison", "approx", "high", False, 1,
       lambda env, a: env.clip(a, env.enc(_CLIP_LO), env.enc(_CLIP_HI)),
       lambda np, a: np.clip(a, _CLIP_LO, _CLIP_HI),
       note=f"截断到 [{_CLIP_LO},{_CLIP_HI}];阈值需加密传入(env.enc)。"),

    # ---------- sort（高代价；常配合授权解密）----------
    Op("sort", "sort", "approx", "high", True, 1,
       lambda hp, a: hp.sort(a), lambda np, a: np.sort(a),
       note="升序排序(基于比较,极贵)。TOP-N/排名地基;大数据量建议授权解密后排。"),

    # ---------- binning ----------
    Op("digitize", "binning", "approx", "high", False, 1,
       lambda env, a: env.digitize(a, env.enc(_BINS)),
       lambda np, a: np.digitize(a, _BINS).astype(float),
       note=f"按阈值 {_BINS} 分箱(分箱点需加密传入)。RFM/ABC 用。"),

    # ---------- math(近似:多项式)----------
    Op("sqrt", "math", "approx", "high", False, 1,
       lambda hp, a: hp.sqrt(a), lambda np, a: np.sqrt(a),
       note="开方(近似)。"),
    Op("exp", "math", "approx", "high", False, 1,
       lambda hp, a: hp.exp(a), lambda np, a: np.exp(a),
       note="指数(近似,输入范围敏感)。"),
    Op("log", "math", "approx", "high", False, 1,
       lambda hp, a: hp.log(a), lambda np, a: np.log(a),
       note="自然对数(近似,要求正数)。"),

    # ---------- 合成算子(用可靠原语补出坏掉/缺失的能力,见 synth.py)----------
    Op("gt", "comparison", "approx", "high", False, 2,
       lambda env, a, b: synth.gt(env, a, b),
       lambda np, a, b: (a > b).astype(float),
       note="a>b 掩码(sign 合成,替代坏掉的 greater)。", impl="synth"),
    Op("lt", "comparison", "approx", "high", False, 2,
       lambda env, a, b: synth.lt(env, a, b),
       lambda np, a, b: (a < b).astype(float),
       note="a<b 掩码(替代坏掉的 less)。", impl="synth"),
    Op("gt_thr", "comparison", "approx", "high", False, 1,
       lambda env, a: synth.gt_threshold(env, a, 2.0),
       lambda np, a: (a > 2.0).astype(float),
       note="a>明文阈值(此处 2.0)掩码。阈值筛选地基。", impl="synth"),
    Op("between", "comparison", "approx", "high", False, 1,
       lambda env, a: synth.between(env, a, 0.0, 3.0),
       lambda np, a: ((a > 0.0) & (a < 3.0)).astype(float),
       note="0<a<3 区间掩码(两阈值掩码相乘)。", impl="synth"),
    Op("sumif_gt", "aggregation", "approx", "high", False, 1,
       lambda env, a: synth.sumif_gt(env, a, 2.0),
       lambda np, a: float(np.sum(a * (a > 2.0))),
       note="条件求和 SUMIF:sum(a where a>2.0)。", impl="synth"),
    Op("countif_gt", "aggregation", "approx", "high", False, 1,
       lambda env, a: synth.countif_gt(env, a, 2.0),
       lambda np, a: float(np.sum(a > 2.0)),
       note="条件计数 COUNTIF:count(a>2.0)。", impl="synth"),
    Op("bin_index", "binning", "approx", "high", False, 1,
       lambda env, a: synth.bin_index(env, a, [0.0, 2.0, 4.0]),
       lambda np, a: np.digitize(a, [0.0, 2.0, 4.0]).astype(float),
       note="分箱序号(替代坏掉的 digitize)。RFM/ABC 用。", impl="synth"),
]


def by_id(op_id: str) -> Optional[Op]:
    for op in REGISTRY:
        if op.id == op_id:
            return op
    return None


def by_category(cat: str) -> list[Op]:
    return [op for op in REGISTRY if op.category == cat]
