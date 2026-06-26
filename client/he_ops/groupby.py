"""
密态 group-by 聚合 —— Block B / Phase B1。

业务最常问的"按维度汇总"(按大区/客户/产品 求和、求均值、计数)。

关键事实(B0 spike 实测):
  · CipherArray 不支持花式/布尔索引(c[[0,2]] / c[mask] 报错);
  · 但 hp.mul(密文, 明文0/1向量) 支持且精确 → 分组用"明文掩码乘 + 求和"实现;
  · 维度键是明文 metadata、组大小 n 明文已知 → 组均值 = 组和 × (1/n) 为**精确**
    (明文常数乘),不走近似密态除法。

隐私不变量:维度键本就是明文(从不进 LLM、从不加密),度量值全程保持密文,
只在最终授权后解密。本模块**不引入任何新的明文泄露**。

记号:第一个参数 hp 是 henumpy(或属性委托到它的对象,如 parity._Env)。
返回:{组值: 密文标量}(count 返回明文 int),组顺序按键排序,确定。
"""
from __future__ import annotations

from typing import Sequence

# 组外掩码移位常量:max/min 时把组外元素压到极端值,使 hp.max/min 只在组内取
_BIG = 1e9


def _unique_sorted(keys: Sequence) -> list:
    """稳定、确定的组顺序:可排序就排序,否则按首次出现。"""
    try:
        return sorted(set(keys))
    except TypeError:
        seen, out = set(), []
        for k in keys:
            if k not in seen:
                seen.add(k); out.append(k)
        return out


def _masks(keys: Sequence):
    """{组值: 明文0/1 掩码向量}。"""
    import numpy as np
    arr = np.asarray(list(keys), dtype=object)
    return {g: (arr == g).astype(np.float64) for g in _unique_sorted(keys)}


def group_sizes(keys: Sequence) -> dict:
    """每组行数(明文,无需 HE)。"""
    return {g: int(m.sum()) for g, m in _masks(keys).items()}


def groupby_sum(hp, cipher_measure, keys: Sequence) -> dict:
    """按 keys 分组求和:sum_g = Σ_{i∈g} value_i(明文掩码乘+求和,精确)。"""
    return {g: hp.sum(hp.mul(cipher_measure, m)) for g, m in _masks(keys).items()}


def groupby_count(keys: Sequence) -> dict:
    """按 keys 分组计数(明文)。"""
    return group_sizes(keys)


def groupby_mean(hp, cipher_measure, keys: Sequence) -> dict:
    """按 keys 分组求均值:mean_g = sum_g × (1/n_g),n_g 明文 → 精确。"""
    out = {}
    for g, m in _masks(keys).items():
        n = int(m.sum())
        s = hp.sum(hp.mul(cipher_measure, m))
        out[g] = hp.mul(s, 1.0 / n) if n else s
    return out


def _masked_extreme(hp, cipher_measure, mask, outside_value: float):
    """组内保留 value,组外置为 outside_value:value*mask + outside_value*(1-mask)。
    hp.add 只接密文操作数,故把组外移位向量加密后相加(明文常量,精确)。"""
    import crypto_toolkit as ct
    inside = hp.mul(cipher_measure, mask)              # 组内=value,组外=0(密文)
    out_vec = (outside_value * (1.0 - mask)).astype(float)  # 组内=0,组外=outside_value(明文)
    return hp.add(inside, ct.encrypt(out_vec))


def groupby_max(hp, cipher_measure, keys: Sequence) -> dict:
    """按 keys 分组最大值(近似:依赖密态 hp.max;组外掩码压到 -BIG 不参与取最大)。
    注意:近似算子,精度不及解密后取;大数据量/大量级建议授权解密后算。"""
    return {g: hp.max(_masked_extreme(hp, cipher_measure, m, -_BIG))
            for g, m in _masks(keys).items()}


def groupby_min(hp, cipher_measure, keys: Sequence) -> dict:
    """按 keys 分组最小值(近似:依赖密态 hp.min;组外掩码抬到 +BIG 不参与取最小)。"""
    return {g: hp.min(_masked_extreme(hp, cipher_measure, m, +_BIG))
            for g, m in _masks(keys).items()}


_AGG = {"sum": groupby_sum, "mean": groupby_mean, "max": groupby_max, "min": groupby_min}


def groupby_agg(hp, cipher_measure, keys: Sequence, agg: str = "sum") -> dict:
    """统一入口。agg ∈ {sum, mean, count, max, min}。"""
    if agg == "count":
        return groupby_count(keys)
    if agg not in _AGG:
        raise ValueError(f"未知聚合 {agg!r};支持 sum/mean/count/max/min")
    return _AGG[agg](hp, cipher_measure, keys)


# ---------- 多维透视 / 层级下钻(复合键)----------
_SEP = "\x1f"   # 维度值拼接分隔符(单元分隔符,业务文本几乎不会含)


def _compound(key_lists: Sequence[Sequence]) -> list[str]:
    """把多列明文维度键按行拼成复合字符串键(numpy 对元组键不友好,故用字符串)。"""
    return [_SEP.join(map(str, row)) for row in zip(*key_lists)]


def pivot_agg(hp, cipher_measure, key_lists: Sequence[Sequence], agg: str = "sum") -> dict:
    """多维透视:key_lists=[大区列, 品类列, ...](明文)。
    返回 {(大区, 品类, ...): 密文标量/计数}。沿用 group-by 掩码法 → 同样精确、同样可扩展到百万级。"""
    res = groupby_agg(hp, cipher_measure, _compound(key_lists), agg)
    return {tuple(k.split(_SEP)): v for k, v in res.items()}


def drilldown_agg(hp, cipher_measure, key_lists: Sequence[Sequence], agg: str = "sum") -> list[dict]:
    """层级下钻:对 [大区, 城市, 门店] 逐层加深,返回每一层的透视结果列表
    [按大区, 按大区×城市, 按大区×城市×门店]。每层一个 {维度元组: 值}。"""
    out = []
    for depth in range(1, len(key_lists) + 1):
        out.append(pivot_agg(hp, cipher_measure, key_lists[:depth], agg))
    return out
