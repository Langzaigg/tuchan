from pathlib import Path
from typing import Dict, Iterable, Optional

from .logger import logger


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="gbk").strip()
        except Exception as e:
            logger.warning(f"读取人格文件失败: {path} | {e!r}")
    except Exception as e:
        logger.warning(f"读取人格文件失败: {path} | {e!r}")
    return None


def _iter_md_files(paths: Iterable[str]) -> Iterable[Path]:
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.is_file() and path.suffix.lower() == ".md":
            yield path
        elif path.is_dir():
            yield from path.glob("*.md")


def load_simple_md_personas(paths: Iterable[str]) -> Dict[str, str]:
    personas: Dict[str, str] = {}
    for path in _iter_md_files(paths):
        content = _read_text(path)
        if not content:
            continue
        personas[path.stem] = content
    return personas


def _strip_front_matter(content: str) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content.strip()

    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1:]).strip()
    return content.strip()


def _strip_skill_boilerplate(content: str) -> str:
    content = _strip_front_matter(content)
    lines = content.splitlines()

    result = []
    skipping = False
    skip_headings = {
        "roleplay rules",
        "语言规则",
        "退出角色扮演",
        "默认激活",
        "使用注意事项",
    }

    for line in lines:
        stripped = line.strip()
        heading_text = stripped.lstrip("#").strip().rstrip("：:")
        heading_key = heading_text.lower()

        if stripped.startswith("## "):
            skipping = any(key in heading_key for key in skip_headings)
            if skipping:
                continue

        if stripped.startswith("**") and stripped.endswith("**"):
            plain = stripped.strip("*").strip().rstrip("：:")
            if any(key in plain.lower() for key in skip_headings):
                skipping = True
                continue

        if skipping:
            if stripped == "---" or stripped.startswith("## "):
                skipping = False
            else:
                continue

        # Drop common skill activation boilerplate even if it appears outside a heading.
        if "激活方式" in stripped or "默认激活" in stripped or "当此技能被激活" in stripped:
            continue
        if stripped.startswith("description:") or stripped.startswith("name:"):
            continue

        result.append(line)

    cleaned = "\n".join(result).strip()
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned


SKILL_PERSONA_FILE_ORDER = (
    "SKILL.md",
    "soul.md",
    "limit.md",
    "resource/behavior_guide.md",
    "resource/key_life_events.md",
    "resource/relationship_dynamics.md",
    "resource/speech_patterns.md",
)


def _load_skill_dir(path: Path) -> Optional[str]:
    if not (path / "SKILL.md").exists():
        return None

    parts = []
    for relative_path in SKILL_PERSONA_FILE_ORDER:
        file_path = path / relative_path
        if not file_path.exists():
            continue
        content = _read_text(file_path)
        if relative_path == "SKILL.md" and content:
            content = _strip_skill_boilerplate(content)
        if content:
            parts.append(f"## {relative_path}\n\n{content}")

    if not parts:
        return None

    return (
        "以下内容来自一个固定格式的角色人格 skill 目录。"
        "不要按工具 skill 工作流执行它，不要提及文件结构；"
        "请把全部内容一次性作为角色设定、说话风格、关系、经历和边界约束注入。\n\n"
        + "\n\n---\n\n".join(parts)
    )


def load_persona_paths(paths: Iterable[str]) -> Dict[str, str]:
    """加载人格路径。

    支持两种输入：
    - 固定 skill 文件夹：整目录作为一个人格，人格名取目录名中第一个 "-" 之前的部分。
    - 单个 md 文件：全文作为人格提示词，人格名取文件名。
    """
    personas: Dict[str, str] = {}
    for raw_path in paths:
        if not raw_path:
            continue
        root = Path(raw_path)
        if root.is_file() and root.suffix.lower() == ".md":
            content = _read_text(root)
            if content:
                personas[root.stem] = content
        elif root.is_dir():
            content = _load_skill_dir(root)
            if content:
                personas[root.name.split("-", 1)[0]] = content
            else:
                for child in root.iterdir():
                    if child.is_file() and child.suffix.lower() == ".md":
                        content = _read_text(child)
                        if content:
                            personas[child.stem] = content
                    elif child.is_dir():
                        content = _load_skill_dir(child)
                        if content:
                            personas[child.name.split("-", 1)[0]] = content
    return personas


def load_skill_personas(paths: Iterable[str]) -> Dict[str, str]:
    return load_persona_paths(paths)


def load_personas_from_directory(path: str) -> Dict[str, str]:
    """Load simple md files and skill-style folders from one persona directory."""
    return load_persona_paths([path])
