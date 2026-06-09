---
name: finance-analysis-skill
description: >
  财务 / 管理会计高级分析方法。
  触发:杜邦分析、ROE 分解、盈亏平衡、保本点、本量利、现金流、营运资金、
  资产周转、净利率、权益乘数、财务比率、偿债能力。
  不适用于:简单求和 / 分组(用 pandaseal 直接做)。
user-invocable: true
metadata: {"openclaw":{"emoji":"💰"}}
---

# finance-analysis — 财务 / 管理会计高级分析

教 AI 用 pandaseal(ps)+ crypto_toolkit(ct)在密文上做财务分析方法。
执行环境已就绪:`cdf`(密文 DataFrame)、`metadata_rows`、`metadata_columns`、
`ps` `ct` `hp` `hl` `pd` `np`,把结果写进 `results = [{sheet_name, df, chart}]`。

## 何时使用

- 杜邦分析(ROE 三因素分解)、盈亏平衡 / 本量利、现金流分析、财务比率体系。

## 核心方法

### 1. 杜邦分析(ROE 分解)
```
ROE = 净利率 × 总资产周转率 × 权益乘数
    = (净利润/营收) × (营收/总资产) × (总资产/净资产)
```

### 2. 盈亏平衡(本量利 CVP)
```
盈亏平衡销量 = 固定成本 / (单价 − 单位变动成本)
盈亏平衡收入 = 固定成本 / 边际贡献率
边际贡献率 = (营收 − 变动成本) / 营收
安全边际率 = (实际销量 − 保本销量) / 实际销量
```

### 3. 财务比率
```
毛利率 = (营收 − 营业成本) / 营收
营业利润率 = 营业利润 / 营收
资产周转率 = 营收 / 总资产
流动比率 = 流动资产 / 流动负债
```

## 代码生成模板(杜邦为例)

```python
df = ct.decrypt_df(cdf)                       # 解密(会触发授权)
meta = pd.DataFrame(metadata_rows)[[c for c in metadata_columns]]
full = pd.concat([meta.reset_index(drop=True), df.reset_index(drop=True)], axis=1)

full["净利率"]   = full["净利润"] / full["营业收入"]
full["资产周转率"] = full["营业收入"] / full["总资产"]
full["权益乘数"] = full["总资产"] / full["净资产"]
full["ROE"]     = full["净利率"] * full["资产周转率"] * full["权益乘数"]

results = [{
    "sheet_name": "杜邦分析",
    "df": full.sort_values("ROE", ascending=False),
    "chart": {"type": "bar", "x": metadata_columns[0], "y": "ROE", "title": "ROE 杜邦分解"},
}]
```

## 硬性规则

1. 不写 import / 初始化(环境已就绪)。
2. 列名严格用 schema 里的字段名(含括号单位)。
3. 比率列保留小数(渲染端会自动按列名格式化百分比)。
4. 字段缺失(如没有"净资产")→ 在 summary 说明缺什么,别硬编。
