---
name: customer-analytics-skill
description: >
  客户分析方法:同期群留存、客户终身价值、复购、流失分析。
  触发:cohort、同期群、留存率、留存曲线、复购率、客户终身价值、LTV、CLV、
  生命周期、客户流失、活跃度、新老客户。
  不适用于:简单客户分组求和(用 pandaseal)。
user-invocable: true
metadata: {"openclaw":{"emoji":"👥"}}
---

# customer-analytics — 客户分析方法

教 AI 用 pandaseal 在密文上做客户留存 / 价值分析。
执行环境已就绪:`cdf` / `metadata_rows` / `metadata_columns` / `ps ct hp hl pd np`,
结果写进 `results = [{sheet_name, df, chart}]`。

## 何时使用

- 同期群(cohort)留存分析、客户终身价值 LTV、复购率、流失识别。

## 核心方法

### 1. 同期群留存(Cohort Retention)
按"首次购买月"把客户分群(cohort),统计每群在后续各月的留存(仍有购买)比例。
```
留存率[cohort, n月后] = 该 cohort 在 n 月后仍活跃的客户数 / 该 cohort 总客户数
```

### 2. 客户终身价值(LTV / CLV)
```
LTV = 平均客单价 × 年复购频次 × 客户生命周期(年)
简化:LTV = 历史累计消费 / 已合作年数 × 预期生命周期
```

### 3. 复购率 / 流失
```
复购率 = 购买≥2 次的客户数 / 总客户数
流失:最近一次购买距今 > 阈值(如 90 天)
```

## 代码生成模板(cohort 留存为例)

```python
df = ct.decrypt_df(cdf)
meta = pd.DataFrame(metadata_rows)[[c for c in metadata_columns]]
full = pd.concat([meta.reset_index(drop=True), df.reset_index(drop=True)], axis=1)

# 假设 full 有:客户、购买月(YYYY-MM)
full["首购月"] = full.groupby("客户")["购买月"].transform("min")
# 月份差(简化:按字符串排序的序号差)
months = sorted(full["购买月"].unique())
idx = {m: i for i, m in enumerate(months)}
full["月差"] = full["购买月"].map(idx) - full["首购月"].map(idx)

cohort = full.groupby(["首购月", "月差"])["客户"].nunique().reset_index()
base = full.groupby("首购月")["客户"].nunique().rename("基数")
cohort = cohort.merge(base, on="首购月")
cohort["留存率"] = cohort["客户"] / cohort["基数"]
pivot = cohort.pivot(index="首购月", columns="月差", values="留存率").reset_index()

results = [{"sheet_name": "同期群留存", "df": pivot, "chart": None}]
```

## 硬性规则

1. 不写 import / 初始化。
2. 留存 / 复购 / 流失率列保留小数(自动按列名格式化为百分比)。
3. 缺关键字段(如没有客户 ID 或购买日期)→ summary 说明,别硬编。
