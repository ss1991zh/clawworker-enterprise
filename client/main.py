"""
客户端 CLI 入口(architecture.md §B 系列)。

环境变量:
- AGENT_BACKEND=real|stub          切换 HE 工具的 backend(默认 stub)
- AGENT_AUTO_APPROVE=1             跳过解密交互授权(自动化/演示用)

最小可用命令:
- agent-client login     登录到主机,缓存 session
- agent-client ingest    把明文数据加密入库(场景 5)
- agent-client ask       发起一次分析(场景 1-4 / 6)
- agent-client tools     列出已加载工具与场景
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from client.excel_output import make_excel_path
from client.keystore import Keystore
from client.llm_client import HTTPLLMClient
from client.local_storage import LocalStorage
from client.permissions import AutoApproveAuthorizer, InteractiveAuthorizer, SessionAuthorizer
from client.skill_workflow import build_workflow
from client.tools import HELearn, HENumpy, HETorch, PandaSeal, ZFHE

app = typer.Typer(help="agent-system 客户端(MVP)")

SESSION_FILE = Path.home() / ".agent-system" / "session.json"


# ---------------------------------------------------------------------------
# 工具初始化
# ---------------------------------------------------------------------------


def _select_backend() -> str:
    """环境变量 AGENT_BACKEND=stub|real 决定走哪个 backend。"""
    import os

    return os.environ.get("AGENT_BACKEND", "stub").lower()


def _load_session() -> dict:
    if not SESSION_FILE.exists():
        raise typer.BadParameter("未登录,请先执行 `agent-client login`")
    return json.loads(SESSION_FILE.read_text(encoding="utf-8"))


def _build_workflow(username: str, host_url: str, token: str):
    backend = _select_backend()
    ks = Keystore()
    keys = ks.get_paths(username)
    sk_path = keys.sk_path if keys else None
    evk_path = keys.evk_path if keys else None

    zfhe = ZFHE(backend=backend, sk_path=sk_path, evk_path=evk_path)
    return build_workflow(
        llm_client=HTTPLLMClient(host_url=host_url, session_token=token),
        zfhe=zfhe,
        pandaseal=PandaSeal(backend=backend, evk_path=evk_path),
        henumpy=HENumpy(backend=backend, evk_path=evk_path),
        helearn=HELearn(backend=backend, evk_path=evk_path),
        hetorch=HETorch(backend="stub", evk_path=evk_path),  # hetorch2 真实包未到位
        authorizer=(
            AutoApproveAuthorizer()
            if os.environ.get("AGENT_AUTO_APPROVE") == "1"
            else SessionAuthorizer(InteractiveAuthorizer())
        ),
    )


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


@app.command()
def login(
    host_url: str = typer.Option(..., "--host", help="主机 URL,如 https://mac-mini.local:8443"),
    username: str = typer.Option(..., prompt=True),
    password: str = typer.Option(..., prompt=True, hide_input=True),
):
    """登录主机,缓存 session token。"""
    import httpx

    resp = httpx.post(
        f"{host_url}/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    if resp.status_code != 200:
        typer.echo(f"登录失败:{resp.status_code} {resp.text}", err=True)
        raise typer.Exit(1)
    body = resp.json()

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps(
            {
                "host_url": host_url,
                "username": username,
                "token": body["token"],
                "expires_at": body["expires_at"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    typer.echo(f"登录成功,token 保存到 {SESSION_FILE}")


@app.command()
def ingest(
    file: Path = typer.Argument(..., help="待加密的明文文件(全数字列)"),
    meta: Optional[Path] = typer.Option(
        None,
        "--meta",
        help="可选:与数据行 1-1 对齐的明文标识列文件(CSV / XLSX),"
        "会保存为 <cipher>.meta.csv 作为 sidecar,工作流自动发现",
    ),
):
    """
    场景 5:把明文文件加密入库。

    若同时提供 --meta(身份/标签列,如姓名/大区/月份等非敏感字段),
    会保存为 <cipher>.meta.csv 紧邻密文文件,工作流自动使用,产出更丰富的 Excel。
    """
    sess = _load_session()
    username = sess["username"]
    backend = _select_backend()
    ks = Keystore()
    keys = ks.get_paths(username)
    sk_path = keys.sk_path if keys else None
    evk_path = keys.evk_path if keys else None
    zfhe = ZFHE(backend=backend, sk_path=sk_path, evk_path=evk_path)

    storage = LocalStorage()
    suffix = file.suffix if backend == "real" else f"{file.suffix}.cipher"
    dst = storage.ciphertext_dir / (file.stem + "_enc" + suffix)
    zfhe.encrypt_file(file, dst)
    typer.echo(f"已加密入库: {dst}")
    typer.echo(f"backend: {backend}")

    # 处理 metadata sidecar
    if meta is not None:
        if not meta.exists():
            typer.echo(f"⚠️ meta 文件不存在: {meta}", err=True)
            raise typer.Exit(2)
        meta_dst = dst.with_suffix(dst.suffix + ".meta.csv")
        # 把 meta 统一规范化为 CSV
        if meta.suffix.lower() == ".csv":
            meta_dst.write_bytes(meta.read_bytes())
        elif meta.suffix.lower() in (".xlsx", ".xls"):
            import pandas as pd

            pd.read_excel(meta).to_csv(meta_dst, index=False)
        else:
            typer.echo(f"⚠️ meta 格式不支持: {meta.suffix}", err=True)
            raise typer.Exit(2)
        typer.echo(f"明文标识列 sidecar: {meta_dst}")


@app.command()
def ask(
    query: str = typer.Argument(..., help="自然语言问题"),
    schema_file: Path = typer.Option(..., "--schema", help="schema JSON 文件"),
    ciphertext: Path = typer.Option(..., "--data", help="本地密文文件路径"),
):
    """场景 1-4 / 6:对密文做分析,产出 Excel。"""
    sess = _load_session()
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    wf = _build_workflow(sess["username"], sess["host_url"], sess["token"])

    initial_state = {
        "user_query": query,
        "schema": schema,
        "ciphertext_paths": [str(ciphertext)],
    }
    final = wf.invoke(initial_state)

    if final.get("error") and not final.get("excel_path"):
        typer.echo(f"任务失败:{final['error']}", err=True)
        raise typer.Exit(1)

    typer.echo(final.get("summary_filtered", "(无 summary)"))
    if final.get("excel_path"):
        typer.echo(f"\n结果已写入 Excel:{final['excel_path']}")


@app.command()
def tools():
    """列出已加载的工具与对应场景。"""
    items = [
        ("场景 1 描述性分析", "pandaseal"),
        ("场景 2 数值计算", "henumpy"),
        ("场景 3 经典 ML", "helearn"),
        ("场景 4 DL 推理", "hetorch"),
        ("场景 5 加密入库", "zfhe (独立)"),
        ("场景 6 复合", "pipeline"),
    ]
    for scn, tool in items:
        typer.echo(f"  {scn:<20} → {tool}")


if __name__ == "__main__":
    app()
