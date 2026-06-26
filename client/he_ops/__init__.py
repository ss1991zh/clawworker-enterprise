"""
he_ops —— 密态算子能力层(Phase 0 地基)。

三件事:
- registry:算子能力表(每个算子标注 类别 / 精确还是近似 / 乘法深度代价 / 是否需授权解密 / 明文参照),
  供「多步规划器」判断"这步 HE 能不能做、要多深、要不要中途解密"。
- parity:明文↔密态对拍框架(同一份数据,numpy 跑 + henumpy 跑,解密后比误差),
  给每个算子建立"可信度 + 实测精度"。
- docs:从能力表生成给 LLM 看的算子参考(喂进 skills/,保证 agent 会用、不乱写)。
"""
from client.he_ops.registry import Op, REGISTRY, by_id, by_category  # noqa: F401
