# 测试文档 · MVP 验证计划

> 基于 `~/architecture.md` 与 `~/llm_system_prompt.md` 设计。
> 目标:以可执行的测试用例,逐条验证架构红线、模块边界、端到端链路。

---

## 一、测试分层

```
单元测试 (unit)        各模块独立验证 —— 覆盖率优先
   ↓
集成测试 (integration) 模块间契约、LangGraph 节点串联
   ↓
端到端 (e2e)           完整工作流:LLM → 加密 → 计算 → 解密 → Excel
   ↓
安全验证 (security)    架构红线的对抗式验证 —— 必须 100% 通过
```

---

## 二、运行方式

```bash
cd ~/agent-system
source .venv/bin/activate

# 全部测试
pytest

# 按文件
pytest tests/test_permissions.py -v

# 按标签(后续可加 marker)
pytest -m security
```

当前状态:**57 / 57 通过**。

---

## 三、模块测试矩阵

每行 = 架构里的一个模块,列出对应的测试文件与覆盖项。

| 架构模块 | 测试文件 | 覆盖项 |
|---------|---------|--------|
| A1 证书管理 | `tests/test_host.py` | 有效期内 / 过期校验、过期后导出 |
| A2 用户管理 | `tests/test_host.py` | 创建账户(证书有效)、登录、密码错误、会话校验 |
| A3 LLM 代理 | `tests/test_host.py` | `parse_llm_text` 文本响应解析、缺块报错 |
| A4 任务分发 | (覆盖在 e2e 中) | 由工作流测试间接覆盖 |
| B1 密钥导入 | (TODO,等真实密钥) | 仅 stub 已有 |
| B3 Skill 工作流 | `tests/test_workflow.py` | 场景 1/2 端到端、过滤重试、授权拒绝 |
| B4 工具集 | `tests/test_tools.py` | 五个工具的加解密 + 计算正确性 |
| B5 Excel 输出 | `tests/test_excel.py` | 文件命名、不覆盖、多 sheet、路径白名单 |
| 解密授权(HITL) | `tests/test_permissions.py` | AutoApprove / Deny / Session(只问一次) |
| B6-2 路径白名单 | `tests/test_permissions.py` + `test_excel.py` | 接受 ~/Downloads 内、拒绝外部路径 |
| B6-3 summary 过滤 | `tests/test_permissions.py` | 12 类明文模式覆盖 |
| C1 computation_plan | `tests/test_contract.py` | 6 个场景的字段约束 |
| C2 summary 红线 | (与 B6-3 同) | 同上 |

---

## 四、安全红线对抗测试(必过)

这一部分专门验证架构里写明的红线,**任何一条不过都不能发布**。

### S1. summary 零明文

| 输入 | 期望 |
|------|------|
| `"2024 年 11 月销售额达 120 万,增长 30%。"` | scan 命中,拦截 |
| `"营收 100 万元。"` | 命中 |
| `"增长率 30%。"` | 命中 |
| `"打开第 1 个 sheet 即可。"` | 不命中(短数字、上下文无害) |
| `"已按月份聚合并生成折线图,详见 Excel。"` | 不命中 |

测试位置:`tests/test_permissions.py::TestSummaryFilter`

### S2. Excel 写入只允许 ~/Downloads/

| 输入路径 | 期望 |
|---------|------|
| `~/Downloads/x.xlsx` | 通过 |
| `/tmp/x.xlsx` | PermissionError |
| `~/Documents/x.xlsx` | PermissionError |
| `../../../etc/x.xlsx` | PermissionError |

测试位置:`tests/test_permissions.py::TestExcelPath` + `tests/test_excel.py::test_write_excel_refuses_path_outside_downloads`

### S3. 解密必须用户授权

| 授权器 | 期望 |
|--------|------|
| AutoApprove | 解密放行 → 产出 Excel |
| Deny | 任务终止,无 Excel,error 含"拒绝" |
| Session | 第一次问、之后会话内复用 |

测试位置:`tests/test_workflow.py::test_authorization_denied`、`tests/test_permissions.py::TestDecryptionAuthorizer`

### S4. 证书过期 → 账户失效

| 场景 | 期望 |
|------|------|
| 证书有效,正确密码登录 | 成功,获得 token |
| 证书有效,错误密码登录 | PermissionError("密码") |
| 证书过期,正确密码登录 | PermissionError("证书"),账户被自动 DISABLED |

测试位置:`tests/test_host.py::test_login_auto_disables_when_cert_expires`

### S5. LLM 全程零明文

由设计保证(prompt 强约束 + 客户端从不上传数据):
- system prompt 明确禁止 LLM 看数据(已写入 `~/llm_system_prompt.md`)
- 客户端 LLM 调用只发 `schema + user_query`,不发 `ciphertext_paths` 内容
- 工作流单元测试用 `FixedLLMClient`,记录全部 LLM 调用入参,可断言 user message 不含明文

待补充(下一阶段):专门的"零明文流量"测试,把 FixedLLMClient 改造成"扫描入参中是否含 sample_sales_rows 的任何值"。

---

## 五、端到端用例

### E1. 场景 1 描述性分析全链路
- **用例:** 输入按月销售数据(密文) + "按月份汇总" 问题
- **预期:** Excel 在 ~/Downloads/ 生成,Sheet "MonthlyTrend" 含正确的月度求和,折线图存在,summary 无明文
- **测试:** `tests/test_workflow.py::test_scenario_1_end_to_end`

### E2. 场景 2 相关系数矩阵
- **用例:** 两个完全线性相关的向量 → 计算相关系数矩阵
- **预期:** Excel "Corr" sheet 存在,数据中 corr(x,y) ≈ 1.0
- **测试:** `tests/test_workflow.py::test_scenario_2_correlation`

### E3. summary 重试 → 兜底
- **用例:** Mock LLM 连续三次返回含明文的 summary
- **预期:** LLM 被调三次,最终 summary 是兜底范式,但 Excel 仍按方案产出
- **测试:** `tests/test_workflow.py::test_summary_filter_triggers_retry_then_fallback`

---

## 六、待补测试(等真实组件接入后)

| 测试 | 依赖 | 触发时机 |
|------|------|----------|
| 真实 HE 加解密往返 | zfhe SDK + 真实 sk/evk | 用户提供后 |
| 真实 pandaseal 上的分组聚合 | pandaseal SDK | 用户提供后 |
| HE 性能基准(秒/万行) | 全部 HE 工具 | 性能调优阶段 |
| Anthropic 真实调用 | API key | 用户提供后 |
| 主机 FastAPI 端到端 HTTP | 主机部署 | 集成测试阶段 |
| 证书 X.509 解析 | 真实 .pem 证书 | 用户提供后 |
| 多用户并发(密钥本地隔离生效) | 至少两套密钥 | 用户提供后 |

---

## 七、测试质量门槛

发布到 MVP 之前必须满足:

- [ ] 所有 `pytest` 用例 100% 通过
- [ ] 安全红线(§四)用例 100% 通过
- [ ] 端到端用例(§五)100% 通过
- [ ] 至少手动跑通一次:`agent-client login` → `agent-client ingest` → `agent-client ask`
- [ ] LLM 系统 prompt 与 `~/llm_system_prompt.md` 完全一致
- [ ] PROVIDE_ME.md 中标注的外部组件全部接入并通过对应测试

---

## 八、测试代码组织

```
tests/
├── conftest.py            # 复用的 fixtures(工具实例、临时 ~/Downloads、Mock LLM 等)
├── test_contract.py       # pydantic 模型与场景一致性
├── test_permissions.py    # B6 三条规则
├── test_excel.py          # Excel 写入与图表
├── test_tools.py          # 五个工具 stub 的正确性
├── test_workflow.py       # LangGraph 端到端
└── test_host.py           # 证书 + 用户 + LLM 文本解析
```

每个测试文件顶部都有简短的目的注释。新增测试遵循:
- 单元测试函数名:`test_<被测函数>_<场景>`
- 集成测试用 class 分组(如 `class TestSummaryFilter`)
- 安全相关用例配 docstring 说明对应架构条款
