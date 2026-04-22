import json
import os
import pickle
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nonebot import get_driver
from typing_extensions import Self, override

from .config import PresetConfig, config
from .logger import logger
from .singleton import Singleton
from .store import StoreEncoder, StoreSerializable


driver = get_driver()


@dataclass
class ImpressionData(StoreSerializable):
    """Per-user impression data under one persona."""

    user_id: str = field(default="")
    chat_history: List[str] = field(default_factory=list)
    chat_impression: str = field(default="")

    @override
    def _init_from_dict(self, self_dict: Dict[str, Any]) -> Self:
        super()._init_from_dict(self_dict)
        self.user_id = str(getattr(self, "user_id", "") or "")
        self.chat_history = list(getattr(self, "chat_history", []) or [])
        self.chat_impression = str(getattr(self, "chat_impression", "") or "")
        return self


@dataclass
class ChatMessageData(StoreSerializable):
    """Structured history entry used to build OpenAI-compatible messages."""

    role: str = field(default="user")
    sender: str = field(default="")
    text: str = field(default="")
    images: List[str] = field(default_factory=list)
    content_is_labeled: bool = field(default=False)
    timestamp: float = field(default=0.0)
    triggered: bool = field(default=False)

    @override
    def _init_from_dict(self, self_dict: Dict[str, Any]) -> Self:
        super()._init_from_dict(self_dict)
        self.role = self.role if self.role in {"user", "assistant"} else "user"
        self.sender = str(getattr(self, "sender", "") or "")
        self.text = str(getattr(self, "text", "") or "")
        self.images = list(getattr(self, "images", []) or [])
        self.content_is_labeled = bool(getattr(self, "content_is_labeled", False))
        try:
            self.timestamp = float(getattr(self, "timestamp", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.timestamp = 0.0
        self.triggered = bool(getattr(self, "triggered", False))
        return self


@dataclass
class PresetData(StoreSerializable):
    """Persona state persisted for one chat session."""

    preset_key: str = field(default="")
    bot_self_introl: str = field(default="")
    is_locked: bool = field(default=False)
    is_default: bool = field(default=False)
    is_only_private: bool = field(default=False)

    chat_impressions: Dict[str, ImpressionData] = field(default_factory=dict)
    chat_memory: Dict[str, str] = field(default_factory=dict)
    context_summary: str = field(default="")
    prompt_messages: List[ChatMessageData] = field(default_factory=list)

    @classmethod
    def create_from_config(cls, preset_config: PresetConfig):
        return PresetData(**preset_config.dict())

    def reset_to_default(self, preset_config: Optional[PresetConfig]):
        if preset_config is not None:
            if preset_config.preset_key != self.preset_key:
                raise Exception(
                    f"wrong preset key, expect `{self.preset_key}` but get `{preset_config.preset_key}`"
                )
            self.is_locked = preset_config.is_locked
            self.is_default = preset_config.is_default
            self.is_only_private = preset_config.is_only_private
            self.bot_self_introl = preset_config.bot_self_introl
        else:
            self.is_locked = False
            self.is_default = False
            self.is_only_private = False

        self.chat_impressions.clear()
        self.chat_memory.clear()
        self.context_summary = ""
        self.prompt_messages.clear()

    @override
    def _init_from_dict(self, self_dict: Dict[str, Any]) -> Self:
        super()._init_from_dict(self_dict)
        self.preset_key = str(getattr(self, "preset_key", "") or "")
        self.bot_self_introl = str(getattr(self, "bot_self_introl", "") or "")
        self.is_locked = bool(getattr(self, "is_locked", False))
        self.is_default = bool(getattr(self, "is_default", False))
        self.is_only_private = bool(getattr(self, "is_only_private", False))
        self.chat_memory = dict(getattr(self, "chat_memory", {}) or {})
        self.context_summary = str(getattr(self, "context_summary", "") or "")

        raw_impressions = getattr(self, "chat_impressions", {}) or {}
        self.chat_impressions = {
            str(k): ImpressionData._load_from_dict(v) if isinstance(v, dict) else v
            for k, v in raw_impressions.items()
            if isinstance(v, (dict, ImpressionData))
        }

        raw_messages = getattr(self, "prompt_messages", []) or []
        self.prompt_messages = [
            ChatMessageData._load_from_dict(v) if isinstance(v, dict) else v
            for v in raw_messages
            if isinstance(v, (dict, ChatMessageData))
        ]
        return self


@dataclass
class ChatData(StoreSerializable):
    """Persisted state for one group or private chat session."""

    chat_key: str = field(default="")
    is_enable: bool = field(default=True)
    enable_auto_switch_identity: bool = field(default=config.NG_ENABLE_AWAKE_IDENTITIES)
    active_preset: str = field(default="")
    preset_datas: Dict[str, PresetData] = field(default_factory=dict)
    next_message_index: int = field(default=0)
    chat_image_history: List[Dict[str, Any]] = field(default_factory=list)

    def reset(self):
        self.chat_image_history.clear()
        self.next_message_index = 0
        for k, v in self.preset_datas.items():
            v.reset_to_default(preset_config=config.PRESETS.get(k, None))

    @override
    def _init_from_dict(self, self_dict: Dict[str, Any]) -> Self:
        super()._init_from_dict(self_dict)
        self.chat_key = str(getattr(self, "chat_key", "") or "")
        self.is_enable = bool(getattr(self, "is_enable", True))
        self.enable_auto_switch_identity = bool(
            getattr(self, "enable_auto_switch_identity", config.NG_ENABLE_AWAKE_IDENTITIES)
        )
        self.active_preset = str(getattr(self, "active_preset", "") or "")

        raw_presets = getattr(self, "preset_datas", {}) or {}
        self.preset_datas = {
            str(k): PresetData._load_from_dict(v) if isinstance(v, dict) else v
            for k, v in raw_presets.items()
            if isinstance(v, (dict, PresetData))
        }
        if not self.preset_datas:
            for preset in config.PRESETS.values():
                preset_data = PresetData.create_from_config(preset)
                self.preset_datas[preset_data.preset_key] = preset_data

        if not self.active_preset or self.active_preset not in self.preset_datas:
            default_presets = [p for p in self.preset_datas.values() if p.is_default]
            self.active_preset = default_presets[0].preset_key if default_presets else next(iter(self.preset_datas), "")

        try:
            self.next_message_index = int(getattr(self, "next_message_index", 0) or 0)
        except (TypeError, ValueError):
            self.next_message_index = 0

        raw_image_history = getattr(self, "chat_image_history", []) or []
        self.chat_image_history = [v for v in raw_image_history if isinstance(v, dict)]
        max_seen_index = self.next_message_index
        for item in self.chat_image_history:
            index = item.get("message_index", item.get("history_index"))
            if isinstance(index, int):
                item["message_index"] = index
                item.pop("history_index", None)
                max_seen_index = max(max_seen_index, index + 1)
        self.next_message_index = max_seen_index
        return self


class PersistentDataManager(Singleton["PersistentDataManager"]):
    """Persistent chat data manager."""

    _datas: Dict[str, ChatData] = {}
    _last_save_data_time: float = 0
    _file_path: str
    _inited: bool
    _filename = "naturel_gpt"

    def backup_file(self, suffix: str):
        base_path = config.NG_DATA_PATH
        file_path = os.path.join(base_path, self._filename)
        if not os.path.isfile(f"{file_path}{suffix}"):
            return
        i = 0
        while os.path.exists(f"{file_path}.{suffix}.{i}.bak"):
            i += 1
        try:
            os.rename(f"{file_path}{suffix}", f"{file_path}.{suffix}.{i}.bak")
        except Exception as e:
            logger.warning(f"文件 `{file_path}{suffix}` 备份失败，可能导致数据异常或丢失: {e}")

    def _compatibility_load(self) -> bool:
        base_path = config.NG_DATA_PATH
        file_path = os.path.join(base_path, self._filename)

        if os.path.exists(f"{file_path}.pkl") and os.path.exists(f"{file_path}.json"):
            logger.warning("pkl 文件与 json 同时存在，仅加载当前配置对应的文件")
            return False

        if config.NG_DATA_PICKLE:
            if not os.path.exists(file_path + ".json"):
                return False
        else:
            if not os.path.exists(file_path + ".pkl"):
                return False

        if not config.NG_DATA_PICKLE:
            self._load_from_file_pickle()
            self._file_path = file_path + ".json"
            self.save_to_file(must_save=True)
            self.backup_file(".pkl")
        else:
            self._load_from_file_json()
            self._file_path = file_path + ".pkl"
            self.save_to_file(must_save=True)
            self.backup_file(".json")
        return True

    def _load_from_file_pickle(self):
        file_path = os.path.join(config.NG_DATA_PATH, f"{self._filename}.pkl")
        self._file_path = file_path
        if not os.path.exists(file_path):
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            logger.info("找不到历史数据，初始化成功 (pickle)")
            return

        with open(file_path, "rb") as f:
            raw_datas = pickle.load(f)
        self._datas = {
            k: ChatData._load_from_dict(v.__dict__ if isinstance(v, ChatData) else v)
            for k, v in raw_datas.items()
            if isinstance(v, (dict, ChatData))
        }
        logger.info("读取历史数据成功 (pickle)")

    def _load_from_file_json(self):
        file_path = os.path.join(config.NG_DATA_PATH, f"{self._filename}.json")
        self._file_path = file_path
        if not os.path.exists(file_path):
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            return

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise Exception(f"File `{self._file_path}` load error! Data not dict!")

        self._datas = {
            k: ChatData._load_from_dict(v)
            for k, v in data.items()
            if isinstance(v, dict)
        }
        logger.info("读取历史数据成功")

    def load_from_file(self):
        self._inited = False
        self._datas = {}
        if not self._compatibility_load():
            if config.NG_DATA_PICKLE:
                self._load_from_file_pickle()
            else:
                self._load_from_file_json()
        self._last_save_data_time = 0
        self._inited = True

    @property
    def is_inited(self) -> bool:
        return self._inited

    def _save_to_file_pickle(self):
        with open(self._file_path, "wb") as f:
            pickle.dump(self._datas, f)

    def _save_to_file_json(self):
        with open(self._file_path, mode="w", encoding="utf-8") as fw:
            json.dump(self._datas, fw, ensure_ascii=False, sort_keys=True, indent=2, cls=StoreEncoder)

    def save_to_file(self, must_save: bool = False):
        if not must_save and time.time() - self._last_save_data_time < 60:
            return

        if config.NG_DATA_PICKLE:
            self._save_to_file_pickle()
        else:
            self._save_to_file_json()

        self._last_save_data_time = time.time()
        logger.info("数据保存成功")

    def get_all_chat_keys(self) -> List[str]:
        return list(self._datas.keys())

    def get_all_chat_datas(self) -> List[ChatData]:
        return list(self._datas.values())

    def get_preset_names(self, chat_key: str):
        return self._datas[chat_key].preset_datas.keys() if chat_key in self._datas else []

    def get_or_create_chat_data(self, chat_key: str) -> ChatData:
        if chat_key in self._datas:
            return self._datas[chat_key]

        chat_data = ChatData(chat_key=chat_key)
        for v in config.PRESETS.values():
            preset_data = PresetData.create_from_config(v)
            chat_data.preset_datas[preset_data.preset_key] = preset_data
        self._datas[chat_key] = chat_data
        return chat_data


@driver.on_shutdown
async def _():
    logger.info("正在保存数据，完成前请勿强制结束")
    PersistentDataManager.instance.save_to_file(must_save=True)
    logger.info("保存完成")
