# 密态算子能力参考(对拍实测)

> 由 `registry.py` + `parity_report.json` 自动生成。**只用 ✅ 的算子;⚠ 的当前构建不可靠,规划/codegen 必须绕开。**

- ✅ 实测可用(33):add, sub, mul, negative, square, absolute, div, reciprocal, sum, mean, prod, cumsum, max, min, var, std, median, percentile, sign, maximum, minimum, clip, sort, sqrt, exp, log, gt, lt, gt_thr, between, sumif_gt, countif_gt, bin_index
- ⚠ 当前不可靠(4):**greater, greater_equal, less, digitize** —— 别用,改用等价可靠算子(如用 sort/max/min 取代 greater 比较)。

精度类:exact=仅加减乘(误差≈密文噪声);approx=多项式近似(实测误差见下)。代价=乘法深度粗分级(high 可能很慢)。

## arithmetic

| 算子 | 状态 | 精度类 | 代价 | 需授权解密 | 实测max误差 | 说明 |
|---|---|---|---|---|---|---|
| `add` | ✅ | exact | low | — | 1.8e-15 | 逐元素相加。组合各类业务指标的基础。 |
| `sub` | ✅ | exact | low | — | 3.6e-15 | 逐元素相减。差额/同比环比的基础。 |
| `mul` | ✅ | exact | low | — | 1.8e-15 | 逐元素相乘(消耗 1 层乘法深度)。 |
| `negative` | ✅ | exact | low | — | 4.4e-16 |  |
| `square` | ✅ | exact | low | — | 1.8e-15 | 平方(1 层乘法)。方差/二阶矩用。 |
| `absolute` | ✅ | approx | medium | — | 2.2e-16 | 绝对值(经 sign/平方近似)。 |
| `div` | ✅ | approx | high | — | 8.9e-16 | 除法(牛顿迭代近似,深度高)。回款率/占比类口径。分母明文时优先乘倒数。 |
| `reciprocal` | ✅ | approx | high | — | 1.1e-16 | 取倒数(近似)。 |

## aggregation

| 算子 | 状态 | 精度类 | 代价 | 需授权解密 | 实测max误差 | 说明 |
|---|---|---|---|---|---|---|
| `sum` | ✅ | exact | low | — | 4.4e-16 | 求和。最常用归约。 |
| `mean` | ✅ | exact | low | — | 2.2e-16 | 均值(和乘明文 1/n)。 |
| `prod` | ✅ | exact | medium | — | 8.9e-16 | 连乘(乘法深度随长度增长)。 |
| `cumsum` | ✅ | exact | low | — | 0.0e+00 | 累计求和。 |
| `max` | ✅ | approx | high | 是 | 2.2e-16 | 最大值(基于比较,深度高)。TOP/封顶用;大数据量建议授权解密后取。 |
| `min` | ✅ | approx | high | 是 | 0.0e+00 | 最小值(基于比较)。 |
| `sumif_gt` | ✅ | approx | high | — | 0.0e+00 | 条件求和 SUMIF:sum(a where a>2.0)。 |
| `countif_gt` | ✅ | approx | high | — | 0.0e+00 | 条件计数 COUNTIF:count(a>2.0)。 |

## stats

| 算子 | 状态 | 精度类 | 代价 | 需授权解密 | 实测max误差 | 说明 |
|---|---|---|---|---|---|---|
| `var` | ✅ | exact | medium | — | 1.8e-15 | 方差(E[x²]-E[x]²)。 |
| `std` | ✅ | approx | high | — | 0.0e+00 | 标准差(方差再开方,sqrt 近似)。 |
| `median` | ✅ | approx | high | 是 | 4.4e-16 | 中位数(依赖排序,深度高)。建议授权解密后算。 |
| `percentile` | ✅ | approx | high | 是 | 1.2e-15 | 分位数(P75,依赖排序)。 |

## comparison

| 算子 | 状态 | 精度类 | 代价 | 需授权解密 | 实测max误差 | 说明 |
|---|---|---|---|---|---|---|
| `greater` | ⚠ 不可靠 | approx | high | — | inf | a>b 返回 1/0(近似)。阈值筛选/条件聚合的地基。 |
| `greater_equal` | ⚠ 不可靠 | approx | high | — | inf |  |
| `less` | ⚠ 不可靠 | approx | high | — | inf |  |
| `sign` | ✅ | approx | high | — | 0.0e+00 | 符号函数(多项式近似)。比较/绝对值的底层。 |
| `maximum` | ✅ | approx | high | — | 4.0e-15 | 逐元素取较大者。 |
| `minimum` | ✅ | approx | high | — | 4.4e-16 |  |
| `clip` | ✅ | approx | high | — | 1.1e-16 | 截断到 [0.2,0.8];阈值需加密传入(env.enc)。 |
| `gt` | ✅ | approx | high | — | 2.2e-16 | a>b 掩码(sign 合成,替代坏掉的 greater)。 |
| `lt` | ✅ | approx | high | — | 9.0e-18 | a<b 掩码(替代坏掉的 less)。 |
| `gt_thr` | ✅ | approx | high | — | 2.2e-16 | a>明文阈值(此处 2.0)掩码。阈值筛选地基。 |
| `between` | ✅ | approx | high | — | 4.4e-16 | 0<a<3 区间掩码(两阈值掩码相乘)。 |

## sort

| 算子 | 状态 | 精度类 | 代价 | 需授权解密 | 实测max误差 | 说明 |
|---|---|---|---|---|---|---|
| `sort` | ✅ | approx | high | 是 | 0.0e+00 | 升序排序(基于比较,极贵)。TOP-N/排名地基;大数据量建议授权解密后排。 |

## binning

| 算子 | 状态 | 精度类 | 代价 | 需授权解密 | 实测max误差 | 说明 |
|---|---|---|---|---|---|---|
| `digitize` | ⚠ 不可靠 | approx | high | — | inf | 按阈值 [0.25, 0.5, 0.75] 分箱(分箱点需加密传入)。RFM/ABC 用。 |
| `bin_index` | ✅ | approx | high | — | 4.4e-16 | 分箱序号(替代坏掉的 digitize)。RFM/ABC 用。 |

## math

| 算子 | 状态 | 精度类 | 代价 | 需授权解密 | 实测max误差 | 说明 |
|---|---|---|---|---|---|---|
| `sqrt` | ✅ | approx | high | — | 8.9e-16 | 开方(近似)。 |
| `exp` | ✅ | approx | high | — | 6.1e-14 | 指数(近似,输入范围敏感)。 |
| `log` | ✅ | approx | high | — | 2.4e-15 | 自然对数(近似,要求正数)。 |

## 表级 · pandaseal(CipherDataFrame,真实分析主用层)

对拍实测 14/14 通过。直接在密文 DataFrame 上 `cdf.xxx()`,解密用 `ct.decrypt_df`。

| 操作 | 状态 | 实测max误差 | 说明 |
|---|---|---|---|
| `col_add` | ✅ | 7.1e-15 | 两列相加(派生指标基础)。 |
| `col_sub` | ✅ | 1.6e-14 | 两列相减/差额。 |
| `col_mul` | ✅ | 3.4e-13 | 两列相乘。 |
| `col_div` | ✅ | 4.4e-16 | 两列相除(回款率/占比)。 |
| `col_sum` | ✅ | 2.8e-14 | 列求和。 |
| `col_mean` | ✅ | 3.6e-15 | 列均值。 |
| `df_mean` | ✅ | 1.4e-14 | 整表逐列均值。 |
| `df_var` | ✅ | 3.1e-13 | 逐列方差(ddof=1,pandas 语义)。 |
| `df_std` | ✅ | 7.1e-14 | 逐列标准差。 |
| `df_cumsum` | ✅ | 7.1e-15 | 逐列累计。 |
| `df_max` | ✅ | 6.8e-14 | 逐列最大值。 |
| `df_quantile` | ✅ | 2.8e-13 | 中位数/分位数。 |
| `sort_values` | ✅ | 7.1e-15 | 按列排序(排名基础)。 |
| `col_gt` | ✅ | 0.0e+00 | 列比较 a>b(pandaseal 原生,返回布尔)。 |

## 模型级 · helearn(密文训练 + 预测)

- ✅ `LinearRegression`:密文训练+预测,端到端冒烟验证通过(diabetes,预测有限且量级合理)。
- 可用(见 helearn-skill 文档):`LogisticRegression`、`GradientBoosting*`、`XGB*`、`CipherTree`、聚类。
- 用法:`m.set_params(iterations,w,learningrate)` → `m.fit(X,y)` → `m.predict(X)`;X/y 为密文。
