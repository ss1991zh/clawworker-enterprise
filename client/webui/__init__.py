"""客户端 Web UI(MVP)。

启动:
    uvicorn client.webui:app --host 127.0.0.1 --port 8444

提供 6 个页面 + 后端 API:
  /            概览(session、工具状态、最近任务)
  /login       账号密码登录
  /data        密文文件管理(列表 / 上传加密 / 删除)
  /ask         自然语言提问(选择密文 + 输入问题 + 运行)
  /jobs        历史任务列表 + 详情 + 下载 Excel
  /settings    主机地址 / backend / 自动授权 等本地设置
"""

from .app import app

__all__ = ["app"]
