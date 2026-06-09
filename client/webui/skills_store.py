"""
Skill 管理 —— 内置 skill(只读) + 用户自定义 skill(可增删)。

内置 skill:
  直接读 client.tools.skills.SKILLS 注册表,这些是固化的 Python 模板
  (加密 / 调用加密工具的计算),用户不可删改,只展示。

自定义 skill:
  用户在 UI 里定义的「业务指标 / 公式」,本质是给 LLM 的一条额外提示,
  让它知道"这个企业有这么个口径的指标该怎么算"。
  持久化到 ~/.agent-system/custom_skills.json。
  注入到 system prompt 的「用户自定义指标」段落,LLM + validator 都能用。

自定义 skill 字段:
  id          内部 id
  name        指标 / skill 名(如「边际贡献率」)
  description 什么时候用(给 LLM 判断意图)
  formula     计算公式(如「(销售收入 - 变动成本) / 销售收入」)
  created_at
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


CUSTOM_SKILLS_FILE = Path.home() / ".agent-system" / "custom_skills.json"


# ---------------------------------------------------------------------------
# 内置 skill(只读)
# ---------------------------------------------------------------------------

def builtin_skills() -> list[dict[str, Any]]:
    """从 client.tools.skills.SKILLS 读内置 skill 元数据。"""
    from client.tools.skills import SKILLS
    out = []
    for name, meta in SKILLS.items():
        out.append({
            "name": name,
            "desc": meta.get("desc", ""),
            "tool": meta.get("tool", ""),
            "params": list(meta.get("params", []) or []),
            "builtin": True,
        })
    return out


# ---------------------------------------------------------------------------
# 自定义 skill(可增删)
# ---------------------------------------------------------------------------

@dataclass
class CustomSkill:
    id: str
    name: str
    description: str = ""
    formula: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["builtin"] = False
        return d


class CustomSkillStore:
    """自定义 skill 的 JSON 持久化 CRUD。"""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or CUSTOM_SKILLS_FILE
        self._lock = threading.Lock()
        self._items: dict[str, CustomSkill] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for d in data:
                cs = CustomSkill(
                    id=d.get("id", secrets.token_hex(5)),
                    name=d.get("name", ""),
                    description=d.get("description", ""),
                    formula=d.get("formula", ""),
                    created_at=d.get("created_at", datetime.now().isoformat(timespec="seconds")),
                )
                if cs.name:
                    self._items[cs.id] = cs
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps([asdict(c) for c in self._items.values()], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:
            pass

    def list_all(self) -> list[dict[str, Any]]:
        out = [c.to_dict() for c in self._items.values()]
        out.sort(key=lambda c: c["created_at"])
        return out

    def create(self, *, name: str, description: str = "", formula: str = "") -> CustomSkill:
        name = (name or "").strip()
        if not name:
            raise ValueError("skill 名不能为空")
        # 不能和内置 skill 重名
        builtin_names = {s["name"] for s in builtin_skills()}
        if name in builtin_names:
            raise ValueError(f"「{name}」是内置 skill 名,请换一个")
        with self._lock:
            # 同名自定义查重(覆盖语义:同名直接更新)
            for c in self._items.values():
                if c.name == name:
                    c.description = description.strip()
                    c.formula = formula.strip()
                    self._save()
                    return c
            sid = secrets.token_hex(5)
            while sid in self._items:
                sid = secrets.token_hex(5)
            cs = CustomSkill(id=sid, name=name,
                             description=description.strip(), formula=formula.strip())
            self._items[sid] = cs
            self._save()
            return cs

    def delete(self, skill_id: str) -> bool:
        with self._lock:
            if skill_id not in self._items:
                return False
            self._items.pop(skill_id)
            self._save()
            return True


# ---------------------------------------------------------------------------
# 注入 system prompt
# ---------------------------------------------------------------------------

def build_custom_skills_prompt_block(custom: list[dict[str, Any]]) -> str:
    """把自定义 skill 拼成一段 prompt,追加到 system prompt 末尾。"""
    if not custom:
        return ""
    lines = [
        "",
        "═══════════════════════════════════════════",
        "用户自定义指标 / 公式(本企业口径 · 优先级高于通用公式表)",
        "═══════════════════════════════════════════",
        "当用户问题命中下列指标名时,**必须**按这里给的公式派生(用 row_detail.compute):",
        "",
    ]
    for c in custom:
        nm = c.get("name", "")
        desc = c.get("description", "")
        formula = c.get("formula", "")
        line = f"★ {nm}"
        if desc:
            line += f" —— {desc}"
        lines.append(line)
        if formula:
            lines.append(f"   公式:{formula}")
    lines.append("")
    return "\n".join(lines)
