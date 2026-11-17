# -*- coding: utf-8 -*-
"""会话（游戏桌 / 旁观 / 先攻 / 轮盘骰 / 日志），对应 DiceSession.cpp。"""

import random
import time
from pathlib import Path
from typing import Dict, List, Optional

from .store import store


class DiceRoulette:
    def __init__(self, face: int, copy: int = 1, pool: Optional[List[int]] = None,
                 rest: Optional[int] = None):
        self.face = face
        self.copy = copy
        if pool is None:
            self.pool = [v for v in range(1, face + 1) for _ in range(copy)]
        else:
            self.pool = list(pool)
        self.siz_res = len(self.pool) if rest is None else rest

    def reset(self):
        self.siz_res = len(self.pool)

    def roll(self) -> int:
        if not self.siz_res:
            self.siz_res = len(self.pool)
        self.siz_res -= 1
        idx = random.randint(0, self.siz_res)
        die = self.pool[idx]
        self.pool[idx] = self.pool[self.siz_res]
        self.pool[self.siz_res] = die
        return die

    def hist(self) -> str:
        out = []
        begin = self.face * self.copy
        while begin > self.siz_res:
            begin -= 1
            out.append(str(self.pool[begin]))
        return " ".join(out)

    def to_json(self) -> dict:
        return {"copy": self.copy, "pool": self.pool, "rest": self.siz_res}

    @classmethod
    def from_json(cls, face: int, obj: dict) -> "DiceRoulette":
        return cls(face, int(obj.get("copy", 1)), obj.get("pool"), int(obj.get("rest", 0)))


class DeckInfo:
    """会话内牌堆实例，对应 DiceSession.h::DeckInfo。"""

    def __init__(self, deck=None, idxs=None, siz_res=None, raw=None):
        if raw is not None:
            self.meta = list(raw.get("meta", []))
            self.idxs = list(raw.get("idxs", []))
            self.siz_res = int(raw.get("size", len(self.idxs)))
            return
        self.meta = list(deck or [])
        self.idxs = list(idxs or [])
        if siz_res is None:
            self.init()
        else:
            self.siz_res = int(siz_res)

    def init(self):
        from .decks import decks

        self.idxs = []
        for idx, card in enumerate(self.meta):
            cnt = 1
            l = card.find("::")
            r = card.find("::", l + 2) if l >= 0 else -1
            if l >= 0 and r >= 0:
                num = decks_draw(card[l + 2:r])
                if len(num) < 4 and num and num[0].isdigit() and num != "0":
                    self.meta[idx] = card[r + 2:]
                    try:
                        cnt = int(num)
                    except ValueError:
                        cnt = 1
            self.idxs.extend([idx] * cnt)
        self.siz_res = len(self.idxs)

    def reset(self):
        self.siz_res = len(self.idxs)

    def draw(self) -> str:
        from .decks import draw_text

        if not self.idxs:
            return ""
        self.siz_res -= 1
        idx = random.randint(0, self.siz_res)
        pick = self.idxs[idx]
        self.idxs[idx] = self.idxs[self.siz_res]
        self.idxs[self.siz_res] = pick
        return draw_text(self.meta[pick])

    def to_json(self) -> dict:
        return {"meta": self.meta, "idxs": self.idxs, "size": self.siz_res}


def decks_draw(exp: str) -> str:
    from .decks import draw_text
    return draw_text(exp)


class Session:
    def __init__(self, name: str):
        self.name = name
        self.t_create = int(time.time())
        self.t_update = self.t_create
        self.areas: List[str] = []
        self.dict: Dict[str, object] = {}
        self.master: List[int] = []
        self.player: List[int] = []
        self.obs: List[int] = []
        self.roulette: Dict[int, DiceRoulette] = {}
        self.decks: Dict[str, DeckInfo] = {}
        self.log = {"logging": False, "start": 0, "name": "", "file": ""}

    # ------------------------------------------------------------- 基础
    def touch(self):
        self.t_update = int(time.time())
        self.save()

    def is_(self, key: str) -> bool:
        return bool(self.dict.get(key))

    def get(self, key: str, default=None):
        if key == "name":
            return self.name
        if key == "gms":
            return list(self.master)
        if key == "pls":
            return list(self.player)
        if key == "obs":
            return list(self.obs)
        if key == "log_name":
            return self.log.get("name", "")
        if key == "log_file":
            return self.log.get("file", "")
        return self.dict.get(key, default)

    def has(self, key: str) -> bool:
        if key in self.dict:
            return True
        return key in ("name", "gms", "pls", "obs", "log_name", "log_file")

    def set(self, key: str, val):
        if key:
            self.dict[key] = val
            self.touch()

    def reset(self, key: str):
        if key in self.dict:
            self.dict.pop(key)
            self.touch()

    def is_simple(self) -> bool:
        return not self.player and not self.master

    def is_part(self, uid) -> bool:
        return (self.is_simple() or int(uid) in self.player or int(uid) in self.master
                or self.is_("auto_join"))

    def roll(self, face: int) -> int:
        if face in self.roulette:
            res = self.roulette[face].roll()
            self.touch()
            return res
        return random.randint(1, face)

    # ------------------------------------------------------------- GM / PL / OB
    def is_gm(self, uid) -> bool:
        return int(uid) in self.master

    def add_gm(self, uid):
        if int(uid) not in self.master:
            self.master.append(int(uid))
            self.touch()

    def del_gm(self, uid) -> bool:
        if int(uid) in self.master:
            self.master.remove(int(uid))
            self.touch()
            return True
        return False

    def is_pl(self, uid) -> bool:
        return int(uid) in self.player

    def add_pl(self, uid) -> bool:
        if int(uid) in self.player:
            return False
        self.player.append(int(uid))
        self.touch()
        return True

    def del_pl(self, uid) -> bool:
        if int(uid) in self.player:
            self.player.remove(int(uid))
            self.touch()
            return True
        return False

    def is_ob(self, uid) -> bool:
        return int(uid) in self.obs

    def del_ob(self, uid) -> bool:
        if int(uid) in self.obs:
            self.obs.remove(int(uid))
            self.touch()
            return True
        return False

    # ------------------------------------------------------------- 表格
    def table_add(self, tab: str, prior, item: str):
        table = self.dict.get(tab)
        if not isinstance(table, dict):
            table = {}
            self.dict[tab] = table
        table[item] = prior
        self.touch()

    def table_del(self, tab: str, item: str) -> bool:
        table = self.dict.get(tab)
        if isinstance(table, dict) and item in table:
            table.pop(item)
            self.touch()
            return True
        return False

    def table_prior_show(self, tab: str) -> str:
        table = self.dict.get(tab)
        if not isinstance(table, dict):
            return ""
        pairs = sorted(table.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
        out = []
        for idx, (key, prior) in enumerate(pairs, 1):
            out.append("\n%d.%s: %s" % (idx, key, prior))
        return "".join(out)

    # ------------------------------------------------------------- 展示
    def show(self) -> str:
        from .formatter import ShowList, print_user

        li = ShowList()
        li.add("桌号: " + self.name)
        if self.has("rule"):
            li.add("规则: " + str(self.get("rule")))
        if self.is_logging():
            li.add("日志记录中:" + str(self.log.get("name", "")))
        if self.master:
            li.add("GM: " + " & ".join(print_user(u) for u in self.master))
        if self.player:
            li.add("PL:" + "".join("\n" + print_user(u) for u in self.player))
        if self.obs:
            li.add("OB:" + "".join("\n" + print_user(u) for u in self.obs))
        if self.roulette:
            li.add("轮盘骰: D" + "/".join(str(f) for f in self.roulette))
        return li.show("\n")

    # ------------------------------------------------------------- 日志
    def is_logging(self) -> bool:
        return bool(self.log.get("logging"))

    def log_dir(self) -> Path:
        d = store.dir / "log"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log_path(self) -> Path:
        return self.log_dir() / str(self.log.get("file", ""))

    def log_append(self, text: str):
        if not self.log.get("logging"):
            return
        try:
            with self.log_path().open("a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    # ------------------------------------------------------------- 持久化
    def to_json(self) -> dict:
        return {
            "name": self.name,
            "create_time": self.t_create,
            "update_time": self.t_update,
            "areas": self.areas,
            "data": self.dict,
            "master": self.master,
            "player": self.player,
            "observer": self.obs,
            "roulette": {str(f): r.to_json() for f, r in self.roulette.items()},
            "decks": {k: v.to_json() for k, v in self.decks.items()},
            "log": self.log,
        }

    @classmethod
    def from_json(cls, obj: dict) -> "Session":
        s = cls(obj.get("name", ""))
        s.t_create = int(obj.get("create_time", 0))
        s.t_update = int(obj.get("update_time", 0))
        s.areas = list(obj.get("areas", []))
        s.dict = dict(obj.get("data", {}))
        s.master = [int(v) for v in obj.get("master", [])]
        s.player = [int(v) for v in obj.get("player", [])]
        s.obs = [int(v) for v in obj.get("observer", [])]
        for face, robj in (obj.get("roulette") or {}).items():
            s.roulette[int(face)] = DiceRoulette.from_json(int(face), robj)
        for key, dobj in (obj.get("decks") or {}).items():
            s.decks[key] = DeckInfo(raw=dobj)
        s.log = dict(obj.get("log") or s.log)
        return s

    def save(self):
        store.save_session(self.name, self.to_json())


class SessionManager:
    def __init__(self):
        self.by_name: Dict[str, Session] = {}
        self.by_chat: Dict[str, Session] = {}
        self.inc = 0
        self._loaded = False

    def load(self):
        if self._loaded:
            return
        self._loaded = True
        for name, obj in store.load_sessions().items():
            try:
                s = Session.from_json(obj)
            except Exception:
                continue
            self.by_name[name] = s
            for chat in s.areas:
                self.by_chat[chat] = s

    @staticmethod
    def chat_key(gid, uid=0) -> str:
        return "g%d" % int(gid) if gid else "u%d" % int(uid)

    def get_if(self, gid, uid=0) -> Optional[Session]:
        self.load()
        return self.by_chat.get(self.chat_key(gid, uid))

    def get(self, gid, uid=0) -> Session:
        self.load()
        key = self.chat_key(gid, uid)
        if key in self.by_chat:
            return self.by_chat[key]
        name = "g%d" % int(gid) if gid else "usr%d" % int(uid)
        s = self.by_name.get(name)
        if s is None:
            s = Session(name)
            s.areas.append(key)
            self.by_name[name] = s
        self.by_chat[key] = s
        return s

    def get_by_name(self, name: str) -> Optional[Session]:
        self.load()
        return self.by_name.get(name)

    def new_game(self, name: str, gid: int, uid: int) -> Session:
        self.load()
        rule = "COC7"
        if "-" in name:
            pos = name.rfind("-")
            while pos != -1:
                prefix = name[:pos]
                if prefix.upper() == "COC7":
                    rule = prefix
                    break
                pos = name.rfind("-", 0, pos)
        self.inc += 1
        g_name = "%s#%d" % (name, self.inc)
        while g_name in self.by_name:
            self.inc += 1
            g_name = "%s#%d" % (name, self.inc)
        s = Session(g_name)
        key = self.chat_key(gid, uid)
        s.areas.append(key)
        s.dict["rule"] = rule
        s.master.append(int(uid))
        self.by_name[g_name] = s
        old = self.by_chat.get(key)
        if old is not None and key in old.areas:
            old.areas.remove(key)
            old.save()
        self.by_chat[key] = s
        s.save()
        return s

    def open(self, session: Session, gid: int, uid: int):
        self.load()
        key = self.chat_key(gid, uid)
        old = self.by_chat.get(key)
        if old is not None and old is not session and key in old.areas:
            old.areas.remove(key)
            old.save()
        if key not in session.areas:
            session.areas.append(key)
        self.by_chat[key] = session
        session.save()

    def close(self, gid: int, uid: int):
        self.load()
        key = self.chat_key(gid, uid)
        s = self.by_chat.pop(key, None)
        if s is not None:
            if key in s.areas:
                s.areas.remove(key)
            s.save()

    def over(self, gid: int, uid: int):
        self.load()
        key = self.chat_key(gid, uid)
        s = self.by_chat.get(key)
        if s is None:
            return
        self.by_name.pop(s.name, None)
        for area in list(s.areas):
            self.by_chat.pop(area, None)
        store.drop_session(s.name)


sessions = SessionManager()
