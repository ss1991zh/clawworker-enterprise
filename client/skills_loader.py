"""
SKILL.md 加载器(Anthropic Agent Skills 渐进式披露格式)。

每个 skill = client/skills/<slug>/ 下的一套文档:
  SKILL.md   frontmatter(name/description/触发条件) + 正文(快速参考 / 硬性规则 / 工作流)
  INDEX.md   API 查找表(可选)
  examples/  可跑示例(可选)
  docs/      逐 API 详解(可选,prompt 里不全量注入)

用途:
  - Skill 管理 UI 展示(parse_frontmatter)
  - 代码生成时按意图路由,把相关 SKILL.md 正文 + INDEX 注入 LLM prompt
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# 内置 SKILL.md(仓库内,只读)
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# 用户拖拽添加的 SKILL.md(沙盒,可删)
USER_SKILLS_DIR = Path.home() / ".agent-system" / "user_skills"


@dataclass
class SkillDoc:
    slug: str                       # 目录名,如 pandaseal-skill
    name: str                       # frontmatter name
    description: str                # frontmatter description(含触发条件)
    user_invocable: bool
    body: str                       # SKILL.md 去 frontmatter 后的正文
    index_md: str = ""              # INDEX.md 全文(可空)
    base_dir: str = ""              # 该 skill 目录绝对路径(用于替换 {baseDir})
    examples: list[str] = field(default_factory=list)  # examples/*.md 全文
    is_user: bool = False           # True=用户拖拽添加(可删) / False=内置(只读)

    def to_meta(self) -> dict:
        """给 UI 用 —— 不含大块正文。"""
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "user_invocable": self.user_invocable,
            "has_index": bool(self.index_md),
            "example_count": len(self.examples),
            "is_user": self.is_user,
            "kind": "skill_md",
        }


# ---------------------------------------------------------------------------
# frontmatter 解析(轻量,不依赖 yaml)
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """返回 (frontmatter_dict, body)。支持 `key: value` 与 `key: >` 多行块。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm_raw, body = m.group(1), m.group(2)

    fm: dict = {}
    lines = fm_raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        mk = re.match(r"^(\w[\w\-]*)\s*:\s*(.*)$", line)
        if not mk:
            i += 1
            continue
        key, val = mk.group(1), mk.group(2).strip()
        if val in (">", "|", ">-", "|-"):
            # 多行块:收集后续缩进行
            block: list[str] = []
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            fm[key] = " ".join(b for b in block if b).strip()
            continue
        fm[key] = val
        i += 1
    return fm, body


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def _load_one(skill_dir: Path, is_user: bool = False) -> Optional[SkillDoc]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return None
    fm, body = parse_frontmatter(text)
    name = fm.get("name", skill_dir.name)
    desc = fm.get("description", "")
    ui = str(fm.get("user-invocable", fm.get("user_invocable", "true"))).lower() in ("true", "1", "yes")

    index_md = ""
    idx = skill_dir / "INDEX.md"
    if idx.exists():
        try:
            index_md = idx.read_text(encoding="utf-8")
        except Exception:
            pass

    examples: list[str] = []
    ex_dir = skill_dir / "examples"
    if ex_dir.is_dir():
        for ex in sorted(ex_dir.glob("*.md")):
            try:
                examples.append(ex.read_text(encoding="utf-8"))
            except Exception:
                pass

    return SkillDoc(
        slug=skill_dir.name,
        name=name,
        description=desc,
        user_invocable=ui,
        body=body.strip(),
        index_md=index_md,
        base_dir=str(skill_dir),
        examples=examples,
        is_user=is_user,
    )


def load_all(skills_dir: Optional[Path] = None) -> list[SkillDoc]:
    """加载内置(client/skills/)+ 用户(~/.agent-system/user_skills/)所有 SKILL.md。
    skills_dir 仅供测试覆盖内置目录。"""
    out: list[SkillDoc] = []
    seen: set[str] = set()
    # 内置
    builtin_root = skills_dir or SKILLS_DIR
    if builtin_root.is_dir():
        for d in sorted(builtin_root.iterdir()):
            if d.is_dir():
                doc = _load_one(d, is_user=False)
                if doc and doc.slug not in seen:
                    out.append(doc); seen.add(doc.slug)
    # 用户
    if USER_SKILLS_DIR.is_dir():
        for d in sorted(USER_SKILLS_DIR.iterdir()):
            if d.is_dir():
                doc = _load_one(d, is_user=True)
                if doc and doc.slug not in seen:
                    out.append(doc); seen.add(doc.slug)
    return out


# ---------------------------------------------------------------------------
# 用户技能:拖拽添加 / 删除
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    import re as _re
    s = _re.sub(r"[^\w一-鿿\-]+", "-", (name or "").strip()).strip("-")
    return (s or "user-skill")[:60]


def add_user_skill_md(content: str, fallback_name: str = "") -> SkillDoc:
    """
    用户拖拽一个 SKILL.md 文本 → 解析 frontmatter → 存到 user_skills/<slug>/SKILL.md。
    必须能解析出 name + description(frontmatter),否则报错。
    """
    fm, _body = parse_frontmatter(content)
    name = fm.get("name") or fallback_name
    if not name:
        raise ValueError("SKILL.md 缺少 frontmatter 的 name 字段")
    if not (fm.get("description")):
        raise ValueError("SKILL.md 缺少 frontmatter 的 description(触发条件)字段")
    slug = _slugify(fm.get("name") or fallback_name)
    # 不能覆盖内置
    builtin_slugs = {d.name for d in SKILLS_DIR.iterdir()} if SKILLS_DIR.is_dir() else set()
    if slug in builtin_slugs:
        slug = slug + "-user"
    dst_dir = USER_SKILLS_DIR / slug
    dst_dir.mkdir(parents=True, exist_ok=True)
    (dst_dir / "SKILL.md").write_text(content, encoding="utf-8")
    doc = _load_one(dst_dir, is_user=True)
    if not doc:
        raise ValueError("SKILL.md 解析失败")
    return doc


def _strip_common_prefix(paths: list[str]) -> str:
    """求一组相对路径的公共顶层目录(如全是 pandaseal-skill/... → 返回 pandaseal-skill/)。"""
    norm = [p.replace("\\", "/").lstrip("/") for p in paths]
    tops = {p.split("/", 1)[0] for p in norm if "/" in p}
    # 只有当所有文件都在同一个顶层目录下才剥离
    if len(tops) == 1 and all("/" in p for p in norm):
        return next(iter(tops)) + "/"
    return ""


def add_user_skill_files(files: list[tuple]) -> SkillDoc:
    """
    用户拖拽一个技能包(多文件嵌套)→ 保留目录结构存到 user_skills/<slug>/。

    files: list[(relpath: str, data: bytes)]
      relpath 是浏览器给的相对路径,如 'pandaseal-skill/docs/read_excel.md'
    必须含一个 SKILL.md(顶层或单层子目录),其 frontmatter 要有 name + description。
    """
    if not files:
        raise ValueError("没有文件")
    # 规整路径 + 找 SKILL.md
    norm = [(p.replace("\\", "/").lstrip("/"), d) for p, d in files]
    skill_entry = next(((p, d) for p, d in norm if p.rstrip("/").endswith("SKILL.md")), None)
    if not skill_entry:
        raise ValueError("技能包里没有 SKILL.md")

    content = skill_entry[1].decode("utf-8", errors="replace")
    fm, _ = parse_frontmatter(content)
    if not fm.get("name"):
        raise ValueError("SKILL.md 缺少 frontmatter 的 name 字段")
    if not fm.get("description"):
        raise ValueError("SKILL.md 缺少 frontmatter 的 description(触发条件)字段")

    slug = _slugify(fm["name"])
    builtin_slugs = {d.name for d in SKILLS_DIR.iterdir()} if SKILLS_DIR.is_dir() else set()
    if slug in builtin_slugs:
        slug = slug + "-user"
    dst_dir = USER_SKILLS_DIR / slug
    # 重新添加同名 → 覆盖
    import shutil
    if dst_dir.exists():
        shutil.rmtree(dst_dir, ignore_errors=True)
    dst_dir.mkdir(parents=True, exist_ok=True)

    prefix = _strip_common_prefix([p for p, _ in norm])
    for p, d in norm:
        rel = p[len(prefix):] if prefix and p.startswith(prefix) else p
        # 路径越权 / 逃逸防御
        if not rel or rel.startswith("..") or "/.." in rel or rel.startswith("/"):
            continue
        target = (dst_dir / rel).resolve()
        if not target.is_relative_to(dst_dir.resolve()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(d)

    doc = _load_one(dst_dir, is_user=True)
    if not doc:
        raise ValueError("技能包写入后 SKILL.md 解析失败")
    return doc


def add_user_skill_zip(zip_bytes: bytes) -> SkillDoc:
    """用户拖拽一个 .zip(完整 skill 包,含 SKILL.md)→ 解压到 user_skills/。"""
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        # 找 SKILL.md(允许在顶层或单层子目录)
        skill_md_path = next((n for n in names if n.rstrip("/").endswith("SKILL.md")), None)
        if not skill_md_path:
            raise ValueError("zip 里没有 SKILL.md")
        content = zf.read(skill_md_path).decode("utf-8", errors="replace")
        fm, _ = parse_frontmatter(content)
        if not fm.get("name") or not fm.get("description"):
            raise ValueError("SKILL.md 缺 name / description")
        slug = _slugify(fm["name"])
        builtin_slugs = {d.name for d in SKILLS_DIR.iterdir()} if SKILLS_DIR.is_dir() else set()
        if slug in builtin_slugs:
            slug = slug + "-user"
        dst_dir = USER_SKILLS_DIR / slug
        dst_dir.mkdir(parents=True, exist_ok=True)
        # 计算 zip 内的公共前缀目录
        prefix = skill_md_path[: -len("SKILL.md")]
        for n in names:
            if n.endswith("/"):
                continue
            if prefix and not n.startswith(prefix):
                continue
            rel = n[len(prefix):] if prefix else n
            if not rel or rel.startswith("..") or rel.startswith("/"):
                continue
            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(n))
    doc = _load_one(dst_dir, is_user=True)
    if not doc:
        raise ValueError("SKILL.md 解析失败")
    return doc


def delete_user_skill(slug: str) -> bool:
    """删除用户技能(只能删 user_skills/ 下的,内置不可删)。"""
    import shutil
    target = USER_SKILLS_DIR / slug
    if not target.is_dir():
        return False
    # 路径越权防御
    if not target.resolve().is_relative_to(USER_SKILLS_DIR.resolve()):
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def list_meta(skills_dir: Optional[Path] = None) -> list[dict]:
    return [d.to_meta() for d in load_all(skills_dir)]


def get_body(slug: str, skills_dir: Optional[Path] = None) -> Optional[str]:
    for d in load_all(skills_dir):
        if d.slug == slug:
            return d.body
    return None


# ---------------------------------------------------------------------------
# 意图路由 —— 给用户问题选相关 skill
# ---------------------------------------------------------------------------

# slug → 触发关键词(中英)。命中即把该 skill 注入代码生成 prompt。
_ROUTE_KEYWORDS: dict[str, list[str]] = {
    "pandaseal-skill": [
        # DataFrame / 表格分析(默认主力)
        "分析", "统计", "汇总", "分组", "排名", "排行", "明细", "占比", "比率", "比例",
        "完成率", "回款率", "毛利", "提成", "贡献", "周转", "均值", "平均", "求和", "总计",
        "筛选", "排序", "合并", "透视", "describe", "groupby", "dataframe", "表",
    ],
    "helearn-skill": [
        "预测", "训练", "模型", "回归", "分类", "聚类", "ml", "machine learning",
        "xgboost", "gbdt", "逻辑回归", "线性回归", "拟合", "推理",
    ],
    "henumpy-skill": [
        "矩阵", "向量", "数组", "线性代数", "点积", "范数", "协方差", "相关系数",
        "傅里叶", "numpy", "数值计算",
    ],
    "hetorch-skill": [
        "神经网络", "深度学习", "nn", "torch", "卷积", "embedding", "推断网络",
    ],
    "zfhe-skill": [
        "加密入库", "加密文件", "encrypt", "密钥",
    ],
}

# 数据分析永远带上 pandaseal(它是 DataFrame 主力);其他按命中追加。
_DEFAULT_SLUG = "pandaseal-skill"


_TRIGGER_RE = re.compile(r"触发(?:条件)?[::]?\s*(?:用户提及\s*)?(.+?)(?:[。\n]|不适用)", re.DOTALL)
_SPLIT_RE = re.compile(r"[、,，/|]+")


def _trigger_terms(desc: str) -> list[str]:
    """从 frontmatter description 抽触发关键词(取"触发…"后那段,按、,分词)。"""
    if not desc:
        return []
    m = _TRIGGER_RE.search(desc)
    seg = m.group(1) if m else desc
    terms = [t.strip() for t in _SPLIT_RE.split(seg) if t.strip()]
    # 过滤太短 / 太长的噪声
    return [t for t in terms if 2 <= len(t) <= 12]


def route(user_query: str, skills_dir: Optional[Path] = None) -> list[SkillDoc]:
    """
    根据用户问题选相关 SkillDoc(至少含 pandaseal)。
    两层匹配:① 核心库 skill 的硬编码关键词(_ROUTE_KEYWORDS)
            ② 任意 skill(含业务方法包 / 用户自定义)的 frontmatter 触发词
    """
    docs = {d.slug: d for d in load_all(skills_dir)}
    if not docs:
        return []
    q = (user_query or "").lower()
    chosen: list[str] = []

    # ① 核心库:硬编码关键词
    for slug, kws in _ROUTE_KEYWORDS.items():
        if slug in docs and any(kw.lower() in q for kw in kws):
            chosen.append(slug)

    # ② 所有 skill:按 description 触发词匹配(业务方法包 / 用户技能由此命中)
    for slug, doc in docs.items():
        if slug in chosen:
            continue
        terms = _trigger_terms(doc.description)
        if any(t.lower() in q for t in terms):
            chosen.append(slug)

    # 保证 pandaseal 兜底在场(DataFrame 主力)
    if _DEFAULT_SLUG in docs and _DEFAULT_SLUG not in chosen:
        chosen.insert(0, _DEFAULT_SLUG)
    # helearn 需要 henumpy 初始化字典,自动带上
    if "helearn-skill" in chosen and "henumpy-skill" in docs and "henumpy-skill" not in chosen:
        chosen.append("henumpy-skill")
    return [docs[s] for s in chosen]
