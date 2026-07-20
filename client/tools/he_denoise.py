"""
同态解密结果去噪 —— 全项目**唯一**的实现,所有解密路径共用。

为什么必须集中:同态解密会把精确 0 解成 ~1e-15、非零值带 ~1e-13 抖动。近零不归零时,
下游所有除零护栏都会失效 —— `replace(0, NaN)` 匹配不到 1e-15,`np.where(x > 0, ...)`
又因 1e-15 > 0 而放行 —— 比率会炸成 -5e19 这种天文数字并混进排名表。

历史教训(优化循环 16→19 轮):去噪最早只加在 skills._decrypt,后来发现 codegen 沙盒、
定时任务批量解密各自还有独立的解密出口,补了一轮又一轮。故收敛到本模块,
并由 tests 里的守门用例保证新增解密出口不会再漏(见 test_all_decrypt_paths_denoised)。
"""
from __future__ import annotations

# |x| < 该阈值视作同态噪声归零。业务金额/计数/得分不会有这么小的真实值,
# 而 HE 的零值噪声量级在 1e-15 左右,两者相差 9 个数量级,阈值取中很安全。
HE_ZERO_EPS = 1e-6


def denoise(obj):
    """把解密结果里的同态近零噪声吸附回 0。

    支持 DataFrame / Series / ndarray;其它类型原样返回。
    去噪失败绝不吞掉解密结果 —— 宁可带噪声,也不能丢数据。
    """
    try:
        import numpy as np
        import pandas as pd

        if isinstance(obj, pd.DataFrame):
            num = obj.select_dtypes(include="number")
            if not num.empty:
                obj[num.columns] = num.mask(num.abs() < HE_ZERO_EPS, 0.0)
            return obj
        if isinstance(obj, pd.Series):
            if pd.api.types.is_numeric_dtype(obj):
                return obj.mask(obj.abs() < HE_ZERO_EPS, 0.0)
            return obj
        if isinstance(obj, np.ndarray) and obj.dtype.kind == "f":
            return np.where(np.abs(obj) < HE_ZERO_EPS, 0.0, obj)
    except Exception:  # noqa: BLE001 —— 去噪是增强,不能反过来破坏解密结果
        pass
    return obj
