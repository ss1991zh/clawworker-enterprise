# A1+B0 Spike 结论(2026-06-26)

实测命令:`AGENT_BACKEND=real python -m client.he_ops._probe`(脚本已删)。

## A1 — helearn 模型 API / 耗时

**数据集**(`hl.datasets.load_*`,返回对象,**train/test 已是密文**):
- `load_diabetes`、`load_boston` → 回归;`load_breast_cancer` → 分类。
- 属性:`feature_names, target_names, train_data, train_target, test_data, test_target, DESCR`。

**模型接口**:
| 模型 | 构造/训练 | 备注 |
|---|---|---|
| LinearRegression | `set_params(iterations,w,learningrate)` + `fit(X,y,calloss=False)` + `predict(X)` | 需传初始权重 `w=hp.ones_array(n_feat+1)` |
| LogisticRegression | 同上 | **predict 返回 logit(w·x),非概率非标签** → 取标签需 `logit>0`;须先标准化特征 |
| GradientBoosting{Classifier,Regressor} | `set_params(**)` + `fit(X,y)` + `predict` | 树模型,无需 w |
| XGB{Classfier,Regressor} | 同上 | 注意类名拼写 `XGBClassfier`(库自带拼写) |
| CipherTree / XgbCipherTree | 仅 `predict(instance)` | **推理-only**(载入预训练树做密文推理) |

**耗时**:LinearRegression fit+predict ≈ 0.0s,LogisticRegression ≈ 0.2s(限 10 迭代)。→ selfcheck 快车道可含线性/逻辑;GBDT/XGB 待 A2 实测耗时再定是否进 `--full`。

**坑**:解释器退出时 `dlsym FreeDoublePtr symbol not found` 是密文对象 GC 析构噪声,**非功能失败**,结果均正常。

## B0 — henumpy 密文索引/掩码

- 密文类型 `CipherArray`。
- `c[i]`、`c[i:j]` 支持;**`c[[0,2,4]]`、`c[bool_mask]` 不支持**(ValueError: Unsupported indexing operation)。
- **`hp.mul(密文, 明文0/1向量)` 支持且精确**:`hp.sum(hp.mul(c, mask))` = 90.0(精确)。
- `hp.mul(密文, 密文mask)` 也可(90.0000…13,有噪声)→ 无谓,优先明文掩码。

**对 B1 的结论**:group-by **用明文掩码乘+求和**实现(不用索引)。组和精确;组大小 n 明文 → **组均值 = 组和 × (1/n) 精确**,不走近似密态除法。max/min 需掩码移位(组外置 -BIG)+ 近似 hp.max,标 approx。
