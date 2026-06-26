"""
窗口/时序算子 —— Block B / Phase B2。

环比差额、移动平均、滞后、变化率等时序分析。

实现事实(spike 实测):
  · 密文切片算术可靠:a[k:] - a[:-k] 精确;
  · hp.cumsum 可靠;hp.append 可拼接密文(用于给 cumsum 前缀 0);
  · 故 rolling = ([0]++cumsum) 的切片相减,精确。

精度:diff/lag/rolling_sum/rolling_mean 为**精确**(仅加减 + 明文常数乘);
pct_change 含密态除法,**近似**(标 approx),分母接近 0 时不稳。

对齐 numpy/pandas:返回"valid"长度(不前补 NaN),与 np.diff / pandas.rolling(w).sum().dropna() 一致。
记号:第一个参数 hp 是 henumpy(或属性委托对象)。
"""
from __future__ import annotations


def diff(hp, a, k: int = 1):
    """k 阶差分:out[i] = a[i] − a[i−k],长度 n−k(对齐 np.diff 的 1 阶 / k 阶移位差)。
    环比/同比差额。"""
    return hp.sub(a[k:], a[:-k])


def lag(hp, a, k: int = 1):
    """滞后对齐:返回去掉末尾 k 个的序列(= a[:-k]),与 diff 的被减项对齐(上一期值)。"""
    return a[:-k]


def _cumsum0(hp, a):
    """给 cumsum 前缀一个 0:返回 [0, cs0, cs1, ...](长度 n+1)。"""
    import crypto_toolkit as ct
    import numpy as np
    return hp.append(ct.encrypt(np.array([0.0])), hp.cumsum(a))


def rolling_sum(hp, a, w: int):
    """移动求和(窗口 w,valid 长度 n−w+1):out[k] = Σ a[k..k+w−1]。
    用 ([0]++cumsum) 切片相减,精确。对齐 pandas.rolling(w).sum().dropna()。"""
    cs0 = _cumsum0(hp, a)        # 长度 n+1
    return hp.sub(cs0[w:], cs0[:-w])


def rolling_mean(hp, a, w: int):
    """移动平均(窗口 w):rolling_sum × (1/w),明文常数乘,精确。"""
    return hp.mul(rolling_sum(hp, a, w), 1.0 / w)


def pct_change(hp, a, k: int = 1):
    """变化率:(a[i] − a[i−k]) / a[i−k],长度 n−k。**近似**(密态除法);分母近 0 时不稳。
    环比/同比增长率。分母为明文时应改用乘倒数。"""
    return hp.div(diff(hp, a, k), a[:-k])
