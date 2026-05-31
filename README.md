# Clawworker Enterprise

**多用户加密数据分析 Agent · LangGraph 实现**

在用户本地用同态加密做数据分析,LLM 全程只见 schema 不见明文,结果以 Excel 交付。
基于 zionskill 工具链(crypto_toolkit / pandaseal / henumpy / helearn / hetorch)+
LangGraph 工作流,适合需要"数据不出本机"的合规场景(财务 / 销售 / 医疗 / 私域)。

---

## 架构一览

```
mac 主机(控制面)               用户客户端(数据面 + 执行)
─────────────────────         ─────────────────────────
 证书管理                       密钥导入(本地隔离)
 用户管理(双因子)              本地存储(密文 + Excel)
 LLM 代理                       Skill 工作流(LangGraph)
 任务分发                       工具集(加密层 + 计算层)
                                Excel 输出(产品级渲染)
                                B6 权限护栏(授权/路径/过滤)
```

完整架构、模块边界、6 个场景路由、双通道数据流、产品级渲染规格,
全部见 [`docs/architecture.md`](docs/architecture.md)。

---

## 快速开始

### 1. 环境

```bash
python --version  # 需要 3.11+
cd clawworker-enterprise
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # 客户端 + 开发依赖
pip install -e ".[host]"     # 如需运行主机
```

### 2. 接入真实 HE 工具链(zionskill)

四个 Python 包来自 zionskill 项目(本仓库未包含):

```
crypto_toolkit / henumpy / pandaseal / helearn  → pip install -e 各自的 dev 目录
```

并把外部平台签发的三类文件放到约定位置:

| 文件 | 默认位置 |
|------|----------|
| `skf`(secret key) | `crypto_toolkit-64_dev/crypto_toolkit/file/skf` |
| `dictf`(字典 / 计算密钥) | `henumpy-dev/henumpy/file/dictf` |
| `user_authorization`(账户授权) | `henumpy-dev/henumpy/file/user_authorization` |

详见 [`PROVIDE_ME.md`](PROVIDE_ME.md)。

### 3. 跑测试

```bash
pytest                                    # 默认 stub backend(57 单元 + 5 集成 stub)
AGENT_BACKEND=real pytest                 # real backend 集成测试(需密钥就位)
OPENROUTER_API_KEY=xxx AGENT_BACKEND=real pytest  # 完整端到端(含真实 LLM)
```

### 4. 启动主机

```bash
MODEL_TYPE=openrouter \
OPENROUTER_API_KEY=sk-or-v1-xxxxx \
MODEL_NAME=deepseek/deepseek-v4-pro \
uvicorn host.server:app --host 127.0.0.1 --port 8443
```

浏览器打开 `http://127.0.0.1:8443/docs` 看 admin / 登录 / chat 端点。

### 5. 客户端 CLI

```bash
# 一次性:admin 导入授权 + 建账号(通过 REST 或 admin 脚本)
curl -X POST http://127.0.0.1:8443/admin/authorization/import \
  -d '{"username":"alice","path":"/path/to/user_authorization"}'

curl -X POST http://127.0.0.1:8443/admin/account/create \
  -d '{"username":"alice","password":"<set>"}'

# 用户侧
agent-client login --host http://127.0.0.1:8443
AGENT_BACKEND=real agent-client ingest data.csv --meta data_meta.csv
AGENT_BACKEND=real agent-client ask "对每一行计算 X / Y 的比率" \
  --schema schema.json --data ~/.agent-system/ciphertexts/data_enc.csv
```

输出 Excel 写到 `~/Downloads/analysis_<timestamp>.xlsx`,含:
- 顶部 KPI 卡(平均率 / 达标率 / TOP3 / BOTTOM3)
- 100 行明细(中文表头 + 百分比 + 业务档位涂色)
- 大区汇总 sheet(按销售大区聚合)
- TOP10 / BOTTOM10 排行榜(柱状图按大区色板染色)

---

## 目录结构

```
clawworker-enterprise/
├── client/                  # 用户客户端(数据面)
│   ├── skill_workflow.py    # LangGraph 工作流(整套系统的执行核心)
│   ├── tools/               # 工具集 wrapper(stub + real 双 backend)
│   │   ├── crypto.py        # CryptoToolkit(zfhe 加解密)
│   │   ├── pandaseal.py     # 表格分析(group_by/sum/mean/div/...)
│   │   ├── henumpy.py       # 数值矩阵
│   │   ├── helearn.py       # 经典 ML
│   │   ├── hetorch.py       # DL 推理(stub,待 hetorch2 包)
│   │   └── runtime.py       # initSK + initDict 一次性管理
│   ├── excel_output.py      # 产品级 Excel 渲染(KPI/染色/分层 X)
│   ├── permissions.py       # B6 三条规则
│   ├── keystore.py          # sk + evk + user_authorization 本地隔离
│   └── main.py              # Typer CLI
├── host/                    # mac 主机(控制面)
│   ├── cert_manager.py      # 用户授权管理(原证书)
│   ├── user_manager.py      # 账户 + 密码 + 会话
│   ├── llm_proxy.py         # Anthropic / OpenRouter / OpenAI 兼容
│   └── server.py            # FastAPI 端点
├── shared/                  # 主机 + 客户端共用契约
│   ├── contract.py          # ComputationPlan / LLMResponse / AgentState
│   └── prompts.py           # 加载 LLM 系统 prompt
├── tests/                   # 单元 / 集成 / 端到端
└── docs/
    ├── architecture.md      # 完整架构(必读)
    ├── llm_system_prompt.md # LLM 系统 prompt(权威来源)
    └── test_plan.md         # 测试设计与运行指南
```

---

## 设计哲学

1. **数据不出本机** — 明文数据从加密入库到 Excel 解密渲染,全程在用户本地
2. **LLM 零明文上下文** — 只发 schema + 用户意图,不发任何数据行
3. **B6 三条客户端硬规则** —
   解密前用户授权 / Excel 路径白名单(`~/Downloads/`)/ LLM 回答内容过滤(零明文)
4. **拒绝授权不等于失败** — 仍产出 Excel,数据为序列化密文,持有密钥者后续可解
5. **双通道数据流** — 明文标识列(姓名/大区/...)与加密数值列并行,renderer 按行号合并
6. **产品级输出** — 中文化 / 百分比格式 / 业务档位涂色 / KPI 卡 / 大区色板 / 多 sheet

---

## License

MIT(代码) · 但 zionskill 工具链与同态加密库另有授权,见各包内 LICENSE。
