"""
合成算子 —— 用"实测可靠"的原语,补出当前构建里坏掉/缺失的能力。

背景(Phase 0 对拍实测):本构建里 `hp.greater/greater_equal/less`、`hp.digitize` 不可靠,
但 `sign / sub / add(标量) / mul(标量与密文) / sum` 全部可靠(误差 ~1e-15)。
比较/条件聚合/分箱的本质就是 sign + 掩码,故全部可由可靠原语合成。

边界(tie)语义:`sign(0)=0` 会让"恰好等于阈值"的值落到 0.5。为对齐 numpy/Excel,
引入极小 EPS 把边界确定化:
  · 严格 `>` / `<`(sumif/countif/筛选):等于阈值 → 排除(tie→0),与 numpy `a>t` 一致;
  · 包含 `>=`(分箱 bin_index):等于下边界 → 归入上一档(tie→1),与 np.digitize(right=False) 一致。
代价:距阈值 ±EPS(1e-6)内的值可能误判;业务数据(金额/比率)量级远大于此,无实际影响。

记号:布尔掩码用 ~1=True、~0=False(浮点)。第一个参数 hp 是 henumpy 模块(或属性委托到它的对象)。
"""
from __future__ import annotations

EPS = 1e-6


# ---------- 内部:把 (x - t) 的符号变成掩码 ----------
def _mask_strict_pos(hp, diff):
    """diff>0 → 1,diff<=0 → 0(严格大于)。掩码 = (sign(diff-EPS)+1)/2。"""
    return hp.mul(hp.add(hp.sign(hp.add(diff, -EPS)), 1.0), 0.5)


def _mask_incl_pos(hp, diff):
    """diff>=0 → 1,diff<0 → 0(大于等于)。掩码 = (sign(diff+EPS)+1)/2。"""
    return hp.mul(hp.add(hp.sign(hp.add(diff, EPS)), 1.0), 0.5)


# ---------- 比较(密文 vs 密文)----------
def gt(hp, a, b):
    """a > b(严格)→ 掩码。"""
    return _mask_strict_pos(hp, hp.sub(a, b))


def lt(hp, a, b):
    """a < b(严格)→ 掩码 = b>a 严格。"""
    return _mask_strict_pos(hp, hp.sub(b, a))


def ge(hp, a, b):
    """a >= b → 掩码。"""
    return _mask_incl_pos(hp, hp.sub(a, b))


def le(hp, a, b):
    """a <= b → 掩码。"""
    return _mask_incl_pos(hp, hp.sub(b, a))


# ---------- 比较(密文 vs 明文阈值)----------
def gt_threshold(hp, a, t: float):
    """a > 明文阈值 t(严格)→ 掩码。阈值是已知常量,无需加密。"""
    return _mask_strict_pos(hp, hp.add(a, -float(t)))


def lt_threshold(hp, a, t: float):
    """a < 明文阈值 t(严格)→ 掩码。"""
    return _mask_strict_pos(hp, hp.add(hp.negative(a), float(t)))


def ge_threshold(hp, a, t: float):
    """a >= 明文阈值 t → 掩码。"""
    return _mask_incl_pos(hp, hp.add(a, -float(t)))


def between(hp, a, lo: float, hi: float):
    """lo < a < hi(两端严格)→ 掩码。"""
    return hp.mul(gt_threshold(hp, a, lo), lt_threshold(hp, a, hi))


# ---------- 条件聚合 ----------
def sumif_gt(hp, a, t: float):
    """sum(a where a>t)。条件求和(SUMIF,严格大于)。"""
    return hp.sum(hp.mul(a, gt_threshold(hp, a, t)))


def sumif_lt(hp, a, t: float):
    return hp.sum(hp.mul(a, lt_threshold(hp, a, t)))


def countif_gt(hp, a, t: float):
    """count(a>t)。条件计数(COUNTIF,严格大于)。"""
    return hp.sum(gt_threshold(hp, a, t))


def countif_lt(hp, a, t: float):
    return hp.sum(lt_threshold(hp, a, t))


def sum_masked(hp, a, mask):
    """按外部掩码求和(掩码来自 gt/lt/between 等)。"""
    return hp.sum(hp.mul(a, mask))


# ---------- 多条件:掩码布尔代数(掩码 ∈ {0,1}) ----------
def band(hp, m1, m2):
    """逻辑与 AND = m1·m2。"""
    return hp.mul(m1, m2)


def bor(hp, m1, m2):
    """逻辑或 OR = m1 + m2 − m1·m2。"""
    return hp.sub(hp.add(m1, m2), hp.mul(m1, m2))


def bnot(hp, m):
    """逻辑非 NOT = 1 − m。"""
    return hp.add(hp.negative(m), 1.0)


def _reduce(hp, masks, op):
    acc = masks[0]
    for m in masks[1:]:
        acc = op(hp, acc, m)
    return acc


def all_of(hp, masks):
    """多个条件同时成立(AND 链)。"""
    return _reduce(hp, masks, band)


def any_of(hp, masks):
    """任一条件成立(OR 链)。"""
    return _reduce(hp, masks, bor)


def sumif_and(hp, a, masks):
    """多条件同时成立时求和:sum(a where 所有条件成立)。
    例:回款额>50万 且 区域掩码=华东 → sumif_and(hp, 回款, [gt_thr掩码, 区域掩码])。"""
    return hp.sum(hp.mul(a, all_of(hp, masks)))


def sumif_or(hp, a, masks):
    """任一条件成立时求和:sum(a where 任一条件成立)。"""
    return hp.sum(hp.mul(a, any_of(hp, masks)))


def countif_and(hp, masks):
    """多条件同时成立的计数。"""
    return hp.sum(all_of(hp, masks))


def countif_or(hp, masks):
    """任一条件成立的计数。"""
    return hp.sum(any_of(hp, masks))


# ---------- 排名 / top-k(比较和,替代不可靠的近似 sort) ----------
# 原理:rank(xᵢ)=#{j: xⱼ<xᵢ}(升序 0 基)。用 xᵢ 广播减整列 + 严格正掩码求和,
# 比近似 sort 稳。注意:密文逻辑长度不透明(len 给的是填充槽数),故需显式传 n。
# 不拼密文数组(hp.append 拼标量不可靠),按元素累加标量。
def rank(hp, a, n: int) -> list:
    """每个元素的升序排名(0 基)列表:rank[i]=#{j: a[j]<a[i]}。n=逻辑元素个数。
    返回 n 个密文标量(读取需授权解密,会泄露顺序)。"""
    return [hp.sum(_mask_strict_pos(hp, hp.sub(a[i:i + 1], a))) for i in range(n)]


def topk_sum(hp, a, k: int, n: int):
    """最大 k 个元素之和(密文标量)。**隐私友好**:全程不暴露"谁是 top-k"/顺序,只出和。
    = Σ_i a[i]·[rank(a[i]) ≥ n−k]。n=逻辑元素个数。"""
    total = None
    for i in range(n):
        rank_i = hp.sum(_mask_strict_pos(hp, hp.sub(a[i:i + 1], a)))   # #{a[j]<a[i]}
        mask_i = _mask_incl_pos(hp, hp.add(rank_i, -float(n - k)))      # rank_i ≥ n−k
        term = hp.mul(a[i:i + 1], mask_i)
        total = term if total is None else hp.add(total, term)
    return total


def topk_mean(hp, a, k: int, n: int):
    """最大 k 个元素的均值 = topk_sum × (1/k),明文常数乘,精确。"""
    return hp.mul(topk_sum(hp, a, k, n), 1.0 / k)


def bottomk_sum(hp, a, k: int, n: int):
    """最小 k 个元素之和(隐私友好)= Σ_i a[i]·[rank(a[i]) < k]。"""
    total = None
    for i in range(n):
        rank_i = hp.sum(_mask_strict_pos(hp, hp.sub(a[i:i + 1], a)))
        mask_i = _mask_strict_pos(hp, hp.add(-rank_i, float(k)))        # rank_i < k
        term = hp.mul(a[i:i + 1], mask_i)
        total = term if total is None else hp.add(total, term)
    return total


# ---------- 分箱(替代不可靠的 digitize)----------
def bin_index(hp, a, edges: list[float]):
    """分箱序号(0..len(edges)),对齐 np.digitize(a, edges, right=False):
    序号 = Σ_i [a >= edges[i]](下边界包含,故用 ge)。"""
    acc = ge_threshold(hp, a, float(edges[0]))
    for e in edges[1:]:
        acc = hp.add(acc, ge_threshold(hp, a, float(e)))
    return acc
