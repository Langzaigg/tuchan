# -*- coding: utf-8 -*-
"""本地持久化：用户、角色卡、群设置、每日人品、会话。"""

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from .card import CharaCard, Player


def _data_dir() -> Path:
    try:
        from nonebot import get_data_dir
        base = Path(get_data_dir())
    except Exception:  # pragma: no cover - 非 nonebot 环境
        base = Path("data")
    d = base / "dice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        pass


class Store:
    def __init__(self):
        self.dir = _data_dir()
        self._users: Optional[Dict[str, dict]] = None
        self._players: Optional[Dict[str, Player]] = None
        self._chats: Optional[Dict[str, dict]] = None
        self._daily: Optional[dict] = None
        self._dirty = set()
        self._last_flush = 0.0

    # ------------------------------------------------------------------ 懒加载
    @property
    def users(self) -> Dict[str, dict]:
        if self._users is None:
            self._users = _read(self.dir / "users.json", {})
        return self._users

    @property
    def chats(self) -> Dict[str, dict]:
        if self._chats is None:
            self._chats = _read(self.dir / "chats.json", {})
        return self._chats

    @property
    def players(self) -> Dict[str, Player]:
        if self._players is None:
            raw = _read(self.dir / "players.json", {})
            self._players = {k: Player.from_json(v) for k, v in raw.items()}
        return self._players

    @property
    def daily(self) -> dict:
        if self._daily is None:
            raw = _read(self.dir / "daily.json", {})
            if raw.get("date") != _today():
                raw = {"date": _today(), "jrrp": {}}
            self._daily = raw
        return self._daily

    # ------------------------------------------------------------------ 刷盘
    def mark(self, name: str):
        self._dirty.add(name)

    def flush(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_flush < 1.0:
            return
        self._last_flush = now
        if "users" in self._dirty and self._users is not None:
            _write(self.dir / "users.json", self._users)
        if "chats" in self._dirty and self._chats is not None:
            _write(self.dir / "chats.json", self._chats)
        if "players" in self._dirty and self._players is not None:
            _write(self.dir / "players.json",
                   {k: v.to_json() for k, v in self._players.items()})
        if "daily" in self._dirty and self._daily is not None:
            _write(self.dir / "daily.json", self._daily)
        self._dirty.clear()

    def flush_all(self):
        for name in ("users", "chats", "players", "daily"):
            self._dirty.add(name)
        self._last_flush = 0.0
        self.flush(force=True)

    # ------------------------------------------------------------------ 用户
    def user(self, uid) -> dict:
        key = str(int(uid))
        u = self.users.get(key)
        if u is None:
            u = {"trust": 0, "nicks": {}, "conf": {}, "created": int(time.time())}
            self.users[key] = u
            self.mark("users")
        return u

    def trust(self, uid) -> int:
        if not uid:
            return 0
        return int(self.user(uid).get("trust", 0))

    def set_trust(self, uid, level: int):
        self.user(uid)["trust"] = int(level)
        self.mark("users")

    def get_nick(self, uid, gid: int = 0) -> Optional[str]:
        u = self.user(uid)
        nicks = u.get("nicks") or {}
        if str(gid) in nicks:
            return nicks[str(gid)]
        if gid and "0" in nicks:
            return nicks["0"]
        return None

    def nick(self, uid, gid: int = 0) -> Optional[str]:
        u = self.users.get(str(int(uid)))
        if u is None:
            return None
        nicks = u.get("nicks") or {}
        return nicks.get(str(gid), nicks.get("0"))

    def set_nick(self, uid, gid: int, val: str):
        self.user(uid).setdefault("nicks", {})[str(gid)] = val
        self.mark("users")

    def rm_nick(self, uid, gid: int) -> bool:
        nicks = self.user(uid).setdefault("nicks", {})
        if str(gid) in nicks:
            nicks.pop(str(gid))
            self.mark("users")
            return True
        return False

    def clr_nick(self, uid):
        self.user(uid)["nicks"] = {}
        self.mark("users")

    def user_conf(self, uid, key: str, default=None):
        return self.user(uid).setdefault("conf", {}).get(key, default)

    def set_user_conf(self, uid, key: str, val):
        self.user(uid).setdefault("conf", {})[key] = val
        self.mark("users")

    def rm_user_conf(self, uid, key: str):
        self.user(uid).setdefault("conf", {}).pop(key, None)
        self.mark("users")

    # ------------------------------------------------------------------ 角色卡
    def player(self, uid) -> Player:
        key = str(int(uid))
        pl = self.players.get(key)
        if pl is None:
            pl = Player()
            self.players[key] = pl
            self.mark("players")
        return pl

    def has_player(self, uid) -> bool:
        return str(int(uid)) in self.players

    def save_player(self, uid):
        self.player(uid)
        self.mark("players")

    def drop_player(self, uid):
        self.players.pop(str(int(uid)), None)
        self.mark("players")

    # ------------------------------------------------------------------ 群设置
    def chat(self, gid) -> dict:
        key = str(int(gid))
        c = self.chats.get(key)
        if c is None:
            c = {"options": [], "name": "", "created": int(time.time()),
                 "updated": int(time.time())}
            self.chats[key] = c
            self.mark("chats")
        return c

    def chat_name(self, gid) -> str:
        return str(self.chats.get(str(int(gid)), {}).get("name", ""))

    def set_chat_name(self, gid, name: str):
        if name and self.chat_name(gid) != name:
            self.chat(gid)["name"] = name
            self.mark("chats")

    def group_opt(self, gid, option: str) -> bool:
        if not gid:
            return False
        return option in (self.chats.get(str(int(gid)), {}).get("options") or [])

    def set_group_opt(self, gid, option: str):
        c = self.chat(gid)
        opts = c.setdefault("options", [])
        if option not in opts:
            opts.append(option)
            c["updated"] = int(time.time())
            self.mark("chats")

    def reset_group_opt(self, gid, option: str):
        c = self.chat(gid)
        opts = c.setdefault("options", [])
        if option in opts:
            opts.remove(option)
            c["updated"] = int(time.time())
            self.mark("chats")

    def group_welcome(self, gid) -> str:
        return str(self.chats.get(str(int(gid)), {}).get("welcome", ""))

    def set_group_welcome(self, gid, text: Optional[str]):
        c = self.chat(gid)
        if text is None:
            c.pop("welcome", None)
        else:
            c["welcome"] = text
        c["updated"] = int(time.time())
        self.mark("chats")

    def group_rule(self, gid) -> Optional[int]:
        c = self.chats.get(str(int(gid)))
        if c and "rc_rule" in c:
            return int(c["rc_rule"])
        return None

    def set_group_rule(self, gid, rule: int):
        self.chat(gid)["rc_rule"] = int(rule)
        self.mark("chats")

    def rm_group_rule(self, gid):
        c = self.chats.get(str(int(gid)))
        if c and "rc_rule" in c:
            c.pop("rc_rule")
            self.mark("chats")

    # ------------------------------------------------------------------ 每日
    def jrrp_cache(self, uid) -> Optional[int]:
        d = self.daily
        val = (d.get("jrrp") or {}).get(str(int(uid)))
        return None if val is None else int(val)

    def set_jrrp(self, uid, val: int):
        d = self.daily
        d.setdefault("jrrp", {})[str(int(uid))] = int(val)
        self.mark("daily")

    # ------------------------------------------------------------------ 会话
    def session_dir(self) -> Path:
        d = self.dir / "session"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_session(self, name: str, data: dict):
        _write(self.session_dir() / (_safe_name(name) + ".json"), data)

    def load_sessions(self) -> Dict[str, dict]:
        out = {}
        for p in self.session_dir().glob("*.json"):
            obj = _read(p, None)
            if isinstance(obj, dict) and obj.get("name"):
                out[obj["name"]] = obj
        return out

    def drop_session(self, name: str):
        p = self.session_dir() / (_safe_name(name) + ".json")
        try:
            p.unlink()
        except OSError:
            pass


def _safe_name(name: str) -> str:
    bad = '<>:"*|?\\/'
    return "".join("\uff3f" if c in bad else c for c in name) or "_"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


store = Store()
