# 需要你提供的真实组件清单

> 进度跟踪表。每一项接入后这里会标 ✅。

---

## 总览

| # | 组件 | 状态 |
|---|------|------|
| 1 | HE 工具 Python 包(crypto_toolkit / henumpy / pandaseal / helearn) | ✅ 已接入(代码层) |
| 2 | zionskill SKILL 文档 | ✅ 已读取参考 |
| 3 | sk 密钥文件(skf) | ✅ 已就位,真实加解密验证通过 |
| 4 | henumpy 字典(dictf) + 用户授权文件 | ✅ 已就位,initDict 成功(216 天有效期) |
| 5 | LLM API key + 模型名 | ✅ OpenRouter + deepseek/deepseek-v4-pro,端到端跑通 |
| 6 | 测试证书 ≡ user_authorization | ✅ 概念统一,沿用 #4 的 user_authorization |
| 7 | hetorch2 Python 包 | ⏳ 待提供(密态数据分析里目前没有) |

---

## 1. HE 工具 Python 包 ✅

四个真实包已安装到 venv:

```bash
crypto_toolkit  0.2   (位于 /Users/davidzheng/Desktop/密态数据分析/crypto_toolkit-64_dev/)
henumpy         2.1.1 (位于 /Users/davidzheng/Desktop/密态数据分析/henumpy-dev/)
pandaseal       1.0.0 (位于 /Users/davidzheng/Desktop/密态数据分析/pandaseal-dev/)
helearn         0.2   (位于 /Users/davidzheng/Desktop/密态数据分析/helearn-dev/)
```

代码侧已实现 `backend="stub"|"real"` 双模式:

| 工具类 | 文件 | 真实 backend 调用 |
|--------|------|------------------|
| `CryptoToolkit` (别名 `ZFHE`) | `client/tools/crypto.py` | `ct.encrypt/decrypt/encrypt_df/encrypt_csv/encrypt_excel` |
| `HENumpy` | `client/tools/henumpy.py` | `hp.sum/mean/std/var/dot/matmul/corrcoef/cov` |
| `PandaSeal` | `client/tools/pandaseal.py` | `ps.read_csv/read_excel`, `cdf.groupby/mean/sum/...` |
| `HELearn` | `client/tools/helearn.py` | `hl.LinearRegression/LogisticRegression/...`.fit/predict |

默认 backend 是 `stub`,所有原有 57 个测试照常全绿。
`tests/test_integration_real.py` 里有 5 个真实 backend 集成测试,密钥到位后自动启用。

> **术语订正:** 此前文档里把 `zfhe` 当成加密工具名,实际上 `zfhe` 是 zionskill 中的**全流程编排 skill**概念,真正的加解密包叫 `crypto_toolkit`。代码已统一,旧 `ZFHE` 名保留为别名向后兼容。

---

## 2. zionskill SKILL 文档 ✅

已通读 `/Users/davidzheng/Desktop/zionskill-main/`:
- `CLAUDE.md` —— 各 skill 的入口与关键规则
- `zfhe-skill/SKILL.md` —— 决策树(数据源 → 任务 → 工具组合)
- `zfhe-skill/docs/initialization.md` —— 各组合的初始化模板

用法:作为我们 ops 翻译到真实 API 的参考,而非直接喂给 LLM。
(我们的 `~/llm_system_prompt.md` 仍然是 LLM 的系统 prompt。)

---

## 3. sk 密钥文件(skf)⏳

### 需要

一个 `skf` 文件(secret key,用 ct.initSK 加载),格式由 crypto_toolkit 决定。

### 放在哪

```
/Users/davidzheng/Desktop/密态数据分析/crypto_toolkit-64_dev/crypto_toolkit/file/skf
```

(crypto_toolkit 默认从这里读,可在 `RuntimeConfig(sk_path=...)` 覆盖)

### 给我之后

跑这行就能验证生效:

```bash
cd /Users/davidzheng/agent-system && source .venv/bin/activate
AGENT_BACKEND=real pytest tests/test_integration_real.py -v
```

---

## 4. henumpy 字典 + 用户授权文件 ⏳

### 需要

- 至少一个字典文件(hp.initDict 的 dictFilePath)
- `user_authorization` 文件(hp.initDict 的 userFilePath)

### 放在哪

```
/Users/davidzheng/Desktop/密态数据分析/henumpy-dev/henumpy/file/
  ├── <字典文件>
  └── user_authorization
```

### 给我之后

同上跑集成测试即可。

---

## 5. LLM API key + 模型名 ⏳

### 需要

- 模型类型(目前预留 Anthropic Claude;OpenAI / 其他厂商需新加一个 provider)
- API key

### 启动主机时通过环境变量配置

```bash
export MODEL_TYPE=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export MODEL_NAME=claude-sonnet-4-5
uvicorn host.server:app --host 0.0.0.0 --port 8443
```

### 告诉我

1. 模型(Claude / GPT / 其他)
2. API key(我只通过环境变量读,不入代码)
3. 是否需要走特定网关(组织内部代理)

---

## 6. 测试证书 ≡ user_authorization ✅

**术语统一(v3):** 架构里的"证书"等同于 HE 工具链的 `user_authorization` 文件。
不再单独造一份证书 JSON 信封 —— 直接用 #4 提供的 user_authorization。

### 接入后效果

- `host/cert_manager.py` 重构为 `AuthorizationManager`(原名作别名保留)
- `host/user_manager.py::create_account` 不再需要单独的 cert_id,自动按 username 找已导入的授权
- `client/keystore.py` 新增 `import_user_authorization` 与 `KeyPaths.user_auth_path`,每用户一份独立授权
- 真实有效期由 SDK 在 `hp.initDict()` 自带签名+到期校验,主机只做"用户列表 + 失效上报"

### 模式

- **每用户一份** user_authorization(架构标准)
- MVP 阶段只验证有效授权路径;过期路径靠"客户端 init 失败 → 上报主机 → 自动 disable"机制(`AuthorizationManager.report_init_failed`),具体测试留作后续

---

## 7. hetorch2 Python 包 ⏳

密态数据分析目录里目前**没有 hetorch2 包**,所以场景 4(DL 推理)的 real backend 暂未启用,会抛 `NotImplementedError`。

### 给我之后

接入流程同 helearn:
- 装包
- 替换 `client/tools/hetorch.py` 的 `_run_real`
- 加 1-2 个集成测试

---

## 切换 backend 的方法

```python
# 代码内
from client.tools import CryptoToolkit
ct = CryptoToolkit(backend="real")

# 命令行 / pytest 通过环境变量
AGENT_BACKEND=real pytest tests/test_integration_real.py
```

`Runtime` 模块会自动处理 `ct.initSK` / `hp.initDict` 的一次性初始化,缺密钥时给出明确错误,不会让底层 Go 库 panic 把进程崩掉。

---

## 当前测试状态

```
57 passed  ← stub backend 完整链路验证
 5 skipped ← real backend 集成测试(等密钥到位)
```

跑全部:
```bash
cd /Users/davidzheng/agent-system && source .venv/bin/activate && pytest tests/
```
