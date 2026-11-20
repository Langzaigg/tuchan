"""为每个接入点准备 Naturel GPT 的配置，并接入共用工具存储。"""

import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml
from nonebot import logger

from .config import DEFAULT_ONEBOT_ENDPOINT, LEGACY_ENDPOINT_NAME, BotSelfConfig, config, endpoint_name, load_yml

NAS_FIELDS = (
    "NAS_GAME_ROOT_PATH",
    "NAS_GAME_UPLOAD_PATH",
    "NAS_GAME_BASE_URL",
    "NAS_GAME_SYNC_RECORDS_PATH",
)

OVERLAY_HEADER = "# 接入点覆盖层：只写与主配置不同的键，未写的键自动共用主配置（默认 config/naturel_gpt_config.yml）\n"


def nickname_directory(nickname: str) -> str:
    directory = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nickname).strip(" .") or "_"
    if directory != nickname or Path(directory).is_reserved():
        digest = hashlib.sha256(nickname.encode()).hexdigest()[:8]
        directory = f"{directory}_{digest}"
    return directory


def prepare_config(settings) -> Path:
    base_config = Path(getattr(settings, "ng_config_path", "config/naturel_gpt_config.yml"))
    base = (load_yml(base_config) or {}) if base_config.is_file() else {}
    default_onebot = endpoint_name == DEFAULT_ONEBOT_ENDPOINT
    legacy = Path("config/endpoints") / endpoint_name / "naturel_gpt_config.yml"
    if BotSelfConfig.nicknames:
        directory = nickname_directory(BotSelfConfig.nicknames[0])
        target = base_config.parent / "bots" / directory / f"{endpoint_name}.yml"
        if default_onebot:
            legacy = target.with_name(f"{LEGACY_ENDPOINT_NAME}.yml")
    elif default_onebot:
        target = base_config
    else:
        target = legacy

    # 接入点配置文件是覆盖层：只写与主配置不同的键，插件加载时并自主配置。
    overlay_mode = target.resolve() != base_config.resolve()
    if not overlay_mode:
        overlay = None
    elif target.is_file():
        overlay = load_yml(target) or {}
    else:
        from_legacy = legacy.is_file()
        overlay = (load_yml(legacy) or {}) if from_legacy else {}
        if not default_onebot and (not from_legacy or BotSelfConfig.adapter == "qq"):
            overlay["ADMIN_USERID"] = []
            overlay["NAS_GAME_WHITELIST_GROUPS"] = []

    if overlay is not None:
        # 自动精简：与主配置同值的键和 NAS 共用字段从覆盖层剔除（旧全量拷贝由此迁移），
        # 非默认接入点的管理/白名单键例外，始终显式保留以免日后继承主配置的赋值。
        slimmed = {
            key: value
            for key, value in overlay.items()
            if key not in NAS_FIELDS and (key not in base or base[key] != value)
        }
        if not default_onebot:
            for key in ("ADMIN_USERID", "NAS_GAME_WHITELIST_GROUPS"):
                if key in overlay:
                    slimmed[key] = overlay[key]
        if slimmed != overlay or not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            file = NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False)
            temp_path = Path(file.name)
            try:
                with file:
                    file.write(OVERLAY_HEADER)
                    yaml.safe_dump(slimmed, file, allow_unicode=True, sort_keys=False)
                temp_path.replace(target)
            finally:
                temp_path.unlink(missing_ok=True)

    shared = config.get("NaturelGPTShared") or {}
    nas_values = {}
    for key in NAS_FIELDS:
        value = shared.get(key, base.get(key, ""))
        if value and key != "NAS_GAME_BASE_URL":
            value = str(Path(value).resolve())
        nas_values[key] = value

    settings.ng_config_path = str(target.resolve())
    settings.ng_base_config_path = str(base_config.resolve()) if overlay_mode else ""
    settings.ng_shared_nas = nas_values
    settings.ng_persona_dir = str(base_config.resolve().parent / "personas")
    default_data_dir = Path(base.get("NG_DATA_PATH") or "data/naturel_gpt")
    settings.ng_archive_fallback_dir = str(default_data_dir.resolve())
    nickname_source = overlay if overlay is not None else base
    if "nickname" in nickname_source:
        override = nickname_source["nickname"]
        names = (override,) if isinstance(override, str) else tuple(override or ())
        BotSelfConfig.nickname = {str(name).strip() for name in names if name and str(name).strip()}
        settings.nickname = BotSelfConfig.nickname
    if not default_onebot:
        settings.ng_endpoint_data_dir = str((Path("data/endpoints") / endpoint_name / "naturel_gpt").resolve())
        settings.ng_endpoint_proxy = BotSelfConfig.proxy or ""
    shared_draw_db = Path(shared.get("DRAW_DB_PATH") or default_data_dir / "draw.db").resolve()
    settings.ng_shared_draw_db = str(shared_draw_db)
    return shared_draw_db


def import_draw_history(path: Path, primary_db: Path, endpoints_dir: Path) -> None:
    with closing(sqlite3.connect(path, timeout=15)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS imported_draw_databases (source TEXT PRIMARY KEY)")
        candidates = [primary_db, *endpoints_dir.glob("*/naturel_gpt/draw.db")]
        for old_path in candidates:
            source = old_path.resolve()
            if source == path or not source.is_file() or db.execute(
                "SELECT 1 FROM imported_draw_databases WHERE source = ?", (str(source),)
            ).fetchone():
                continue
            try:
                with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as old:
                    rows = old.execute(
                        "SELECT task_id, prompt_data, created_at, updated_at FROM draw_prompts"
                    ).fetchall()
                with db:
                    imported = db.execute("INSERT OR IGNORE INTO imported_draw_databases VALUES (?)", (str(source),))
                    if imported.rowcount:
                        db.executemany("INSERT OR IGNORE INTO draw_prompts VALUES (?, ?, ?, ?)", rows)
            except sqlite3.Error as exc:
                logger.warning(f"读取旧接入点的画图记录失败（{source}）：{exc}")
