---
name: ml-analytics-skill
description: >
  机器学习业务分析:客户/门店聚类分群、回归归因、流失预测分类。
  触发:聚类、K-means、自动分群、无监督、回归归因、因素分析、驱动因素、
  流失预测、流失模型、分类预测、特征重要性、打分模型。
  不适用于:简单时间序列预测(用 forecast_linreg)。
user-invocable: true
metadata: {"openclaw":{"emoji":"🤖"}}
---

# ml-analytics — 机器学习业务分析

教 AI 用 helearn(hl)+ henumpy(hp)+ crypto_toolkit(ct)做密态机器学习。
执行环境已就绪:`cdf` / `metadata_rows` / `metadata_columns` / `ps ct hp hl pd np`,
结果写进 `results = [{sheet_name, df, chart}]`。

标准三段管线:**pandas 清洗 → ct.encrypt 成密态数组 → helearn 训练/推理 → ct.decrypt 取回**。
数值要先归一化(避免 HE 梯度发散):X 除最大值,y 标准化 (x−mean)/std。

## 何时使用

- 客户 / 门店聚类分群(K-means)、回归归因(哪些因素驱动指标)、流失预测分类。

## 核心方法

### 1. K-means 聚类分群
```
把客户按特征(消费、频次、活跃度…)自动分成 K 群,识别相似群体。
```

### 2. 回归归因
```
线性回归系数 → 各因素对目标(如销售额)的边际影响方向与强度。
```

### 3. 流失预测(分类)
```
逻辑回归 / XGB → 输出每个客户的流失概率,排序高风险客户。
```

## 代码生成模板(回归归因为例)

```python
df = ct.decrypt_df(cdf)                       # pandas 清洗
feats = ["广告投入", "门店数", "促销次数"]      # 自变量
target = "销售额"                              # 因变量

X = df[feats].to_numpy(dtype=float)
y = df[target].to_numpy(dtype=float)
# 归一化(防 HE 梯度爆)
x_scale = np.maximum(X.max(axis=0), 1.0)
y_mean, y_std = y.mean(), (y.std() or 1.0)
Xn = X / x_scale
yn = (y - y_mean) / y_std

Xc = ct.encrypt(Xn)                            # 加密成密态数组
yc = ct.encrypt(yn)
model = hl.LinearRegression(iterations=400, learningrate=0.05)
model.fit(Xc, yc)                              # 密态训练

# 系数(若 model 暴露 w),否则用预测对比近似;这里给出方向性归因表
coef = getattr(model, "w", None)
import pandas as pd
attr = pd.DataFrame({"因素": feats, "归一化系数(方向)": (ct.decrypt(coef).flatten()[:len(feats)] if coef is not None else [None]*len(feats))})
results = [{"sheet_name": "回归归因", "df": attr, "chart": None}]
```

## 硬性规则

1. 不写 import / 初始化(`hp.initDict()` / `ct.initSK()` 已做)。
2. **数值必须归一化**再加密,否则 HE 梯度下降发散。
3. helearn 模型:LinearRegression / LogisticRegression / XGBClassfier / XGBRegressor。
4. 样本太少 / 特征共线 → summary 提示结果不稳健。
