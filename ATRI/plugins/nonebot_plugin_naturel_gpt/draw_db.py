import json
import os
import sqlite3
import threading
from typing import Any, Dict, Optional

from .config import config
from .logger import logger

_db_lock = threading.Lock()
_db_path: str = ""


def _get_db_path() -> str:
    """获取数据库文件路径"""
    global _db_path
    if not _db_path:
        base_path = config.NG_DATA_PATH
        os.makedirs(base_path, exist_ok=True)
        _db_path = os.path.join(base_path, "draw.db")
    return _db_path


def _get_connection() -> sqlite3.Connection:
    """获取数据库连接"""
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化数据库表"""
    with _db_lock:
        try:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS draw_prompts (
                    task_id TEXT PRIMARY KEY,
                    prompt_data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            conn.close()
            logger.info(f"绘图提示词数据库初始化成功: {_get_db_path()}")
        except Exception as e:
            logger.error(f"绘图提示词数据库初始化失败: {e}")


def save_prompt(task_id: str, prompt_data: Dict[str, Any]) -> bool:
    """保存绘图提示词，重复编号时覆盖"""
    with _db_lock:
        try:
            conn = _get_connection()
            cursor = conn.cursor()
            prompt_json = json.dumps(prompt_data, ensure_ascii=False)
            cursor.execute("""
                INSERT OR REPLACE INTO draw_prompts (task_id, prompt_data, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (task_id, prompt_json))
            conn.commit()
            conn.close()
            logger.info(f"保存绘图提示词成功: {task_id}")
            return True
        except Exception as e:
            logger.error(f"保存绘图提示词失败: {task_id}, {e}")
            return False


def get_prompt(task_id: str) -> Optional[Dict[str, Any]]:
    """查询绘图提示词"""
    with _db_lock:
        try:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT prompt_data FROM draw_prompts WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return json.loads(row["prompt_data"])
            return None
        except Exception as e:
            logger.error(f"查询绘图提示词失败: {task_id}, {e}")
            return None


def delete_prompt(task_id: str) -> bool:
    """删除绘图提示词"""
    with _db_lock:
        try:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM draw_prompts WHERE task_id = ?", (task_id,))
            conn.commit()
            deleted = cursor.rowcount > 0
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"删除绘图提示词失败: {task_id}, {e}")
            return False


def list_prompts(limit: int = 50) -> list:
    """列出所有绘图提示词（按更新时间倒序）"""
    with _db_lock:
        try:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT task_id, created_at, updated_at FROM draw_prompts ORDER BY updated_at DESC LIMIT ?",
                (limit,)
            )
            rows = cursor.fetchall()
            conn.close()
            return [{"task_id": row["task_id"], "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows]
        except Exception as e:
            logger.error(f"列出绘图提示词失败: {e}")
            return []
