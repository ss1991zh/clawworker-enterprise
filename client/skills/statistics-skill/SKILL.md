---
name: statistics-skill
description: >
  统计分析方法:相关性分析、异常检测、分布与分位数、离散度。
  触发:相关性、相关系数、协方差、关联分析、异常检测、离群点、异常值、
  3σ、IQR、箱线、分位数、分布、标准差、变异系数、可疑数据、突变。
  不适用于:简单 describe(用 pandaseal 的 describe skill)。
user-invocable: true
metadata: {"openclaw":{"emoji":"📊"}}
---

# statistics — 统计分析方法

教 AI 用 pandaseal / numpy 在解密后做统计方法。
执行环境已就绪:`cdf` / `metadata_rows` / `metadata_columns` / `ps ct hp hl pd np`,
结果写进 `results = [{sheet_name, df, chart}]`。

## 何时使用

- 多指标相关性、异常 / 离群值检测、分布与分位数、离散度分析。

## 核心方法

### 1. 相关性分析
```
皮尔逊相关系数矩阵 corr = df[数值列].corr()
判读:|r|>0.7 强相关 · 0.3~0.7 中等 · <0.3 弱
```

### 2. 异常检测
```
3σ 法:|x − 均值| > 3×标准差 → 异常
IQR 法:x < Q1 − 1.5×IQR 或 x > Q3 + 1.5×IQR → 异常(IQR = Q3 − Q1)
```

### 3. 分布 / 分位数
```
P25 / P50(中位数)/ P75 / P90 / P95
变异系数 CV = 标准差 / 均值(跨量纲比离散)
```

## 代码生成模板

### 相关性矩阵
```python
df = ct.decrypt_df(cdf)
num = df.select_dtypes(include="number")
corr = num.corr().reset_index().rename(columns={"index": "指标"})
results = [{"sheet_name": "相关性矩阵", "df": corr, "chart": None}]
```

### 异常检测(IQR)
```python
df = ct.decrypt_df(cdf)
meta = pd.DataFrame(metadata_rows)[[c for c in metadata_columns]]
full = pd.concat([meta.reset_index(drop=True), df.reset_index(drop=True)], axis=1)

col = "金额"     # 要检测的数值列
q1, q3 = full[col].quantile(0.25), full[col].quantile(0.75)
iqr = q3 - q1
lo, hi = q1 - 1.5*iqr, q3 + 1.5*iqr
full["是否异常"] = ((full[col] < lo) | (full[col] > hi)).map({True: "异常", False: "正常"})
outliers = full[full["是否异常"] == "异常"]
results = [{"sheet_name": "异常明细", "df": outliers, "chart": None}]
```

## 硬性规则

1. 不写 import / 初始化。
2. 相关系数 / 变异系数等保留小数。
3. 数据量太少(< 5 行)→ summary 提示样本不足,结果仅供参考。
