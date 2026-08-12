# 网络入口与 TLS 部署方案

## 目标

同时满足:

1. 本机浏览器不再处理自签证书,降低 Windows 安装与换网后的故障率。
2. 任何跨机器通信仍强制 TLS,保护密码、Session Token、授权文件、用户问题和管理操作。
3. Host 的本机 HTTP 与局域网 HTTPS 共享同一个应用进程和内存状态。
4. TLS 初始化失败时 fail-closed,禁止静默降级到局域网 HTTP。

## 端口

| 入口 | 监听 | 协议 | 用途 |
|---|---|---|---|
| Host 本机管理 | `127.0.0.1:8442` | HTTP | 管理主机本机浏览器 |
| Host 局域网 | `0.0.0.0:8443` | HTTPS | 用户端 API、远程 Admin |
| Client 本机界面 | `127.0.0.1:8444` | HTTP | 终端本机浏览器 |

`8442` 与 `8444` 绝不能改绑 `0.0.0.0`。如未来确需远程访问这些入口,必须另加 TLS
反向代理或重新评审安全边界。

## 进程模型

`python -m host.gateway` 在一个进程里创建两个 uvicorn listener,两者共享
`host.server:app`。只有 HTTPS listener 执行 ASGI lifespan,避免启动事件执行两次。

不要用两个独立的 `uvicorn host.server:app` 进程分别监听 8442/8443。账户会话、
Admin 会话、Provider 缓存和 Dispatcher 当前均有内存状态,双进程会造成入口间状态不一致。

## 证书策略

### 有 AD / 企业 CA

推荐给管理主机分配稳定 DNS 名,例如 `clawworker-host.corp.example`,由企业 CA
签发服务器证书,并通过 GPO/MDM 分发根证书。客户端连接固定 DNS 名而不是动态 IP。

### 无企业 CA

保留当前主机自签证书 + 客户端 SPKI/TOFU 锁定:

- 管理主机保存并跨升级复用私钥。
- 首次连接必须人工核对主机与客户端显示的指纹。
- IP/SAN 变化重签时复用同一私钥,客户端按 SPKI 判断仍是原主机。
- 普通用户端不向 Windows Root 证书库导入该证书。
- 远程浏览器若要打开 Admin,应由管理员单独部署企业 CA 或手工建立信任;
  默认建议远程管理关闭、在主机本机使用 `http://127.0.0.1:8442/admin`。

## 启动失败策略

- Host 证书生成/读取失败:gateway 退出非零,supervisor 记录并重试;日志明确显示原因。
- Client 不需要证书,始终只启动 loopback HTTP。
- 禁止“证书失败 → Host 悄悄启动 HTTP”:客户端仍会按 HTTPS 连接,该降级既不安全也不可用。

## 防火墙

- 管理主机仅向企业 LAN 放行 TCP 8443。
- TCP 8442、8444 不需要入站防火墙规则,因为只监听 loopback。
- 建议按企业网段限制 8443 来源,不要暴露到公网。
