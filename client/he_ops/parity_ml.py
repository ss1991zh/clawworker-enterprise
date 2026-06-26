"""
模型级体检(helearn)—— Block A / Phase A2。

逐个密态模型:在标准数据集上 fit+predict,解密后对**留出集真值**算质量(回归 R² / 分类准确率),
并与明文 sklearn 同款模型做基线对照。把"密态 ML"从口号变成有精度凭证、带使用约束的能力。

隐私:体检用的是标准公开数据集(diabetes/breast_cancer),非用户业务数据,且全程本机。
为算 sklearn 基线会在本机解密这些公开数据(不涉及任何用户隐私)。

发现(本 build 实测):
  · LinearRegression(回归)、LogisticRegression(分类)可训练可用;
  · GradientBoosting{Regressor,Classifier}、XGB{Regressor,Classfier} 当前 build 训练内部报错
    'tuple' object does not support item assignment —— 构建缺陷,**不注册**(待库修复);
  · CipherTree/XgbCipherTree 仅 predict(推理-only,需预训练树),不在本"训练体检"内。
  · LogisticRegression.predict 返回 **logit(w·x)**,非概率非标签 → 取标签用 logit>0;
    特征须标准化进 sigmoid 近似有效域(breast_cancer 数据已是 z-score)。

用法:AGENT_BACKEND=real python -m client.he_ops.parity_ml [--save]
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class MlSpec:
    id: str
    task: str          # "regression" | "classification"
    dataset: str       # hl.datasets.load_*
    build: Callable    # (hl, hp, n_feat) -> 已配置模型
    note: str = ""
    pass_floor: float = 0.4   # 回归 R² / 分类 acc 的及格线


def _specs():
    def lin(hl, hp, nf):
        m = hl.LinearRegression(); m.set_params(iterations=100, w=hp.ones_array(nf + 1), learningrate=0.1); return m

    def logi(hl, hp, nf):
        m = hl.LogisticRegression(); m.set_params(iterations=100, w=hp.ones_array(nf + 1), learningrate=0.1); return m

    def gbr(hl, hp, nf):
        m = hl.GradientBoostingRegressor(); m.set_params(n_estimators=5, max_depth=3, learning_rate=0.1); return m

    def gbc(hl, hp, nf):
        m = hl.GradientBoostingClassifier(); m.set_params(n_estimators=5, max_depth=3, learning_rate=0.1); return m

    def xgbr(hl, hp, nf):
        m = hl.XGBRegressor(); m.set_params(n_estimators=5, max_depth=3); return m

    def xgbc(hl, hp, nf):
        m = hl.XGBClassfier(); m.set_params(n_estimators=5, max_depth=3); return m

    return [
        MlSpec("LinearRegression", "regression", "load_diabetes", lin,
               "线性回归(梯度下降,密文训练)。", 0.40),
        MlSpec("LogisticRegression", "classification", "load_breast_cancer", logi,
               "逻辑回归(predict 返回 logit,标签=logit>0;特征须标准化)。", 0.85),
        MlSpec("GradientBoostingRegressor", "regression", "load_diabetes", gbr,
               "GBDT 回归(当前 build 训练报错)。", 0.40),
        MlSpec("GradientBoostingClassifier", "classification", "load_breast_cancer", gbc,
               "GBDT 分类(当前 build 训练报错)。", 0.85),
        MlSpec("XGBRegressor", "regression", "load_diabetes", xgbr,
               "XGBoost 回归(当前 build 训练报错)。", 0.40),
        MlSpec("XGBClassfier", "classification", "load_breast_cancer", xgbc,
               "XGBoost 分类(当前 build 训练报错)。", 0.85),
    ]


@dataclass
class MlResult:
    model: str
    task: str
    passed: bool
    he_score: float          # 密态模型在真值上的 R²/acc
    sk_score: float          # sklearn 基线
    secs: float
    note: str = ""
    error: str = ""


def _r2(np, y, p):
    y, p = np.ravel(y).astype(float), np.ravel(p).astype(float)
    ss_res = float(np.sum((y - p) ** 2)); ss_tot = float(np.sum((y - y.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot else 0.0


def _acc(np, y, label):
    return float(np.mean(np.ravel(y).astype(int) == np.ravel(label).astype(int)))


def _sklearn_baseline(spec, np, Xtr, ytr, Xte, yte):
    try:
        if spec.task == "regression":
            from sklearn.linear_model import LinearRegression as SK
            m = SK().fit(Xtr, ytr); return _r2(np, yte, m.predict(Xte))
        from sklearn.linear_model import LogisticRegression as SK
        m = SK(max_iter=500).fit(Xtr, (ytr > 0.5).astype(int))
        return _acc(np, (yte > 0.5).astype(int), m.predict(Xte))
    except Exception:  # noqa: BLE001
        return float("nan")


def run_spec(spec: MlSpec) -> MlResult:
    import numpy as np
    import crypto_toolkit as ct
    import henumpy as hp
    import helearn as hl
    from client.tools.runtime import Runtime

    t0 = time.time()
    try:
        Runtime.get().ensure_all_initialized()
        d = getattr(hl.datasets, spec.dataset)()
        nf = len(d.feature_names)
        # 明文(本机解密公开数据,算真值与 sklearn 基线)
        Xtr = np.asarray(ct.decrypt(d.train_data)); ytr = np.ravel(np.asarray(ct.decrypt(d.train_target)))
        Xte = np.asarray(ct.decrypt(d.test_data)); yte = np.ravel(np.asarray(ct.decrypt(d.test_target)))
        if spec.task == "classification":
            ytr = (ytr > 0.5).astype(float); yte = (yte > 0.5).astype(float)
        # 密态 fit + predict
        m = spec.build(hl, hp, nf)
        m.fit(d.train_data, d.train_target)
        raw = np.ravel(np.asarray(ct.decrypt(m.predict(d.test_data))))
        if spec.task == "regression":
            he_score = _r2(np, yte, raw[:yte.size])
        else:
            # 分类 predict 输出 (2,n) 布局,前 n 个为 logit;标签 = logit>0
            logits = raw[:yte.size] if raw.size >= yte.size else raw
            he_score = _acc(np, yte, (logits > 0.0).astype(int))
        sk = _sklearn_baseline(spec, np, Xtr, ytr, Xte, yte)
        return MlResult(spec.id, spec.task, he_score >= spec.pass_floor,
                        round(he_score, 4), round(sk, 4), round(time.time() - t0, 2), spec.note)
    except Exception as e:  # noqa: BLE001
        return MlResult(spec.id, spec.task, False, float("nan"), float("nan"),
                        round(time.time() - t0, 2), spec.note, f"{type(e).__name__}: {str(e)[:100]}")


def run_all():
    return [run_spec(s) for s in _specs()]


REPORT = Path(__file__).resolve().parent / "parity_ml_report.json"


def _print(results):
    print(f"{'模型':<28}{'任务':<7}{'通过':<5}{'密态分':<9}{'sklearn':<9}{'秒':<6}备注")
    print("-" * 100)
    ok = 0
    for r in results:
        f = "✓" if r.passed else "✗"
        ok += r.passed
        hs = "—" if r.he_score != r.he_score else f"{r.he_score:.3f}"
        ss = "—" if r.sk_score != r.sk_score else f"{r.sk_score:.3f}"
        tail = r.error or r.note
        print(f"{r.model:<28}{r.task:<7}{f:<5}{hs:<9}{ss:<9}{r.secs:<6}{tail}")
    print("-" * 100)
    print(f"通过 {ok}/{len(results)}  (回归看 R²,分类看准确率;及格线见各模型 pass_floor)")


if __name__ == "__main__":
    import json
    from dataclasses import asdict
    res = run_all()
    _print(res)
    if "--save" in sys.argv:
        REPORT.write_text(json.dumps({r.model: asdict(r) for r in res}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"已写入模型体检报告 → {REPORT.name}")
