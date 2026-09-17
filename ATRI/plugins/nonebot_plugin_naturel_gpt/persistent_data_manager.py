import asyncio
import json
import os
import pickle
import threading
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

# 序列化+写盘在执行器线程中跑时用这把锁串行化，避免并发写同一个 .tmp 文件
_SAVE_LOCK = threading.Lock()


def _is_model_request_error_text(content: str) -> bool:
    if not content:
        return False
    text = str(content).strip()
    lower_text = text.lower()
    return (
        text.startswith("请求大模型时发生错误:")
        or ("runtimeerror('http " in lower_text and "error from provider" in lower_text)
        or ("runtimeerror(\"http " in lower_text and "error from provider" in lower_text)
    )


@dataclass
class ImpressionData(StoreSerializable):
    """Per-user impression data under one persona."""

    user_id: str = field(default="")
    nickname: str = field(default="")
    chat_history: List[str] = field(default_factory=list)
    chat_impression: str = field(default="")

    @override
    def _init_from_dict(self, self_dict: Dict[str, Any]) -> Self:
        super()._init_from_dict(self_dict)
        self.user_id = str(getattr(self, "user_id", "") or "")
        self.nickname = str(getattr(self, "nickname", "") or "")
        self.chat_history = list(getattr(self, "chat_history", []) or [])
        self.chat_impression = str(getattr(self, "chat_impression", "") or "")
        return self


@dataclass
class ChatMessageData(StoreSerializable):
    """Structured history entry used to build OpenAI-compatible messages."""

    role: str = field(default="user")
    user_id: str = field(default="")
    sender: str = field(default="")
    text: str = field(default="")
    images: List[str] = field(default_factory=list)
    content_is_labeled: bool = field(default=False)
    context_only: bool = field(default=False)
    timestamp: float = field(default=0.0)
    triggered: bool = field(default=False)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_id: str = field(default="")
    tool_name: str = field(default="")
    reasoning_content: str = field(default="")
    tool_call_summary: str = field(default="")
    # 印象 system 标记：表示本条 system 消息是某用户的个人印象，注入在该用户触发的 user 消息前。
    # 同一用户在整个上下文中最多注入一次（首次触发时），随其绑定轮次一起被摘要/裁剪删除。
    is_impression: bool = field(default=False)
    impression_user_id: str = field(default="")
    # 每张图片的元数据 [{sender, timestamp}]，与 images 平行；仅 context_only 块使用
    #（块内各行来自不同时间/发送者，图片过期判定需按张而非按块）。普通消息用 item.timestamp。
    image_meta: List[Dict[str, Any]] = field(default_factory=list)

    @override
    def _init_from_dict(self, self_dict: Dict[str, Any]) -> Self:
        super()._init_from_dict(self_dict)
        self.role = self.role if self.role in {"user", "assistant", "tool", "system"} else "user"
        self.user_id = str(getattr(self, "user_id", "") or "")
        self.sender = str(getattr(self, "sender", "") or "")
        self.text = str(getattr(self, "text", "") or "")
        self.images = list(getattr(self, "images", []) or [])
        self.content_is_labeled = bool(getattr(self, "content_is_labeled", False))
        self.context_only = bool(getattr(self, "context_only", False))
        if self.context_only:
            self.role = "system"
        try:
            self.timestamp = float(getattr(self, "timestamp", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.timestamp = 0.0
        self.triggered = bool(getattr(self, "triggered", False))
        self.tool_calls = list(getattr(self, "tool_calls", []) or [])
        self.tool_call_id = str(getattr(self, "tool_call_id", "") or "")
        self.tool_name = str(getattr(self, "tool_name", "") or "")
        self.reasoning_content = str(getattr(self, "reasoning_content", "") or "")
        self.tool_call_summary = str(getattr(self, "tool_call_summary", "") or "")
        self.is_impression = bool(getattr(self, "is_impression", False))
        self.impression_user_id = str(getattr(self, "impression_user_id", "") or "")
        self.image_meta = [m for m in (getattr(self, "image_meta", []) or []) if isinstance(m, dict)]
        # 印象 system 在内存中保留以服务当次运行；持久化时由 PresetData._serializable 过滤掉（不落盘）。
        # 重启后历史轮的印象 system 丢失，下次该用户触发时按最新印象重新注入，避免旧印象残留。
        if self.is_impression:
            self.role = "system"
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
    chat_memory: Dict[str, str] = field(default_factory=dict)  # 群记忆
    user_memories: Dict[str, Dict[str, str]] = field(default_factory=dict)  # 用户个人记忆: {user_id: {key: value}}
    context_summary: str = field(default="")
    tool_call_summary: str = field(default="")  # 模式3: 最近一次工具调用的摘要
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

        self.context_summary = ""
        self.tool_call_summary = ""
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
        
        # 加载用户个人记忆
        raw_user_memories = getattr(self, "user_memories", {}) or {}
        self.user_memories = {}
        for uid, memories in raw_user_memories.items():
            if isinstance(memories, dict):
                self.user_memories[str(uid)] = {str(k): str(v) for k, v in memories.items() if v}
        
        self.context_summary = str(getattr(self, "context_summary", "") or "")
        self.tool_call_summary = str(getattr(self, "tool_call_summary", "") or "")

        raw_impressions = getattr(self, "chat_impressions", {}) or {}
        self.chat_impressions = {
            str(k): ImpressionData._load_from_dict(v) if isinstance(v, dict) else v
            for k, v in raw_impressions.items()
            if isinstance(v, (dict, ImpressionData))
        }

        raw_messages = getattr(self, "prompt_messages", []) or []
        loaded_messages = [
            ChatMessageData._load_from_dict(v) if isinstance(v, dict) else v
            for v in raw_messages
            if isinstance(v, (dict, ChatMessageData))
        ]
        # 轮次不变量按计数而非布尔：user +1、最终 assistant -1，计数为 0 时的 assistant 才是孤儿。
        # 循环邮箱插入会产生 user(A) user(B) assistant(答A) assistant(答B) 的合法序列，
        # 布尔"首条 assistant 关轮"会把答 B 当孤儿丢掉。
        self.prompt_messages = []
        open_users = 0
        for msg in loaded_messages:
            if msg.context_only:
                continue
            if msg.role == "user":
                open_users += 1
                self.prompt_messages.append(msg)
                continue
            if msg.role == "assistant" and open_users > 0 and not msg.tool_calls:
                open_users -= 1
                if _is_model_request_error_text(msg.text):
                    continue
                self.prompt_messages.append(msg)
        return self

    @override
    def _serializable(self) -> Dict[str, Any]:
        """序列化时过滤掉工具消息和思考内容"""
        rtn = super()._serializable()
        # 过滤prompt_messages中的工具消息和思考内容
        if "prompt_messages" in rtn:
            filtered_messages = [
                msg._serializable() if isinstance(msg, ChatMessageData) else msg
                for msg in rtn["prompt_messages"]
                if isinstance(msg, ChatMessageData) and msg.role in {"user", "assistant"} and not msg.tool_calls
            ]
            cleaned_messages = []
            open_users = 0  # 计数版轮次不变量，见 _init_from_dict
            for msg in filtered_messages:
                role = msg.get("role", "") if isinstance(msg, dict) else ""
                if role == "user":
                    open_users += 1
                    cleaned_messages.append(msg)
                elif role == "assistant" and open_users > 0:
                    open_users -= 1
                    if _is_model_request_error_text(str(msg.get("text", "") or "")):
                        continue
                    cleaned_messages.append(msg)
            rtn["prompt_messages"] = cleaned_messages
        return rtn


@dataclass
class ChatData(StoreSerializable):
    """Persisted state for one group or private chat session."""

    chat_key: str = field(default="")
    is_enable: bool = field(default=True)
    enable_auto_switch_identity: bool = field(default=config.NG_ENABLE_AWAKE_IDENTITIES)
    active_preset: str = field(default="")
    active_profile: str = field(default="")  # 当前会话使用的 OpenAI profile
    draw_mode: str = field(default="auto")  # 画图模式: force/on/auto/off
    draw_model: str = field(default="")  # 画图模型：上游工作流名（可选集由 /anima/workflows 动态决定），空 = 动态默认（首选 fuse）
    manga_mode: str = field(default="off")  # 漫画模式: on/off（开启后覆盖 draw_model，使用动态默认工作流）
    manga_style: str = field(default="")    # 漫画模式自定义画风描述
    unlock_content_limit: Optional[bool] = field(default=None)  # 内容限制解锁开关（None=使用配置默认值）
    preset_datas: Dict[str, PresetData] = field(default_factory=dict)
    next_message_index: int = field(default=0)
    chat_image_history: List[Dict[str, Any]] = field(default_factory=list)
    global_memory_enabled: bool = field(default=False)  # 群级 global 记忆开关
    global_chat_memory: Dict[str, str] = field(default_factory=dict)  # global 群记忆（所有人格共享）

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
        self.active_profile = str(getattr(self, "active_profile", "") or "")
        raw_draw_mode = str(getattr(self, "draw_mode", "auto") or "auto")
        self.draw_mode = raw_draw_mode if raw_draw_mode in ("force", "on", "auto", "off") else "auto"
        # 画图模型迁移：旧 turbo_mode(bool) → draw_model(str)
        # 合法取值由上游 /anima/workflows 动态决定，此处不做名单校验；
        # 旧内部名映射与默认值回退由读取侧的 anima_generate.get_draw_model() 完成
        raw_draw_model = str(getattr(self, "draw_model", "") or "")
        if raw_draw_model:
            self.draw_model = raw_draw_model
        else:
            # 旧数据兼容：turbo_mode=True → turbo, False → base；从未设置过 → 留空（读取时动态选默认）
            legacy_turbo = getattr(self, "turbo_mode", None)
            if legacy_turbo is None:
                self.draw_model = ""
            else:
                self.draw_model = "turbo" if legacy_turbo else "base"
        # 清理旧字段
        if hasattr(self, "turbo_mode"):
            try:
                delattr(self, "turbo_mode")
            except Exception:
                pass
        raw_manga = str(getattr(self, "manga_mode", "off") or "off")
        self.manga_mode = raw_manga if raw_manga in ("on", "off") else "off"
        self.manga_style = str(getattr(self, "manga_style", "") or "")
        raw_unlock = getattr(self, "unlock_content_limit", None)
        self.unlock_content_limit = bool(raw_unlock) if raw_unlock is not None else None

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

        self.global_memory_enabled = bool(getattr(self, "global_memory_enabled", False))
        raw_global_chat_mem = getattr(self, "global_chat_memory", {}) or {}
        self.global_chat_memory = {str(k): str(v) for k, v in raw_global_chat_mem.items() if v}
        return self


class PersistentDataManager(Singleton["PersistentDataManager"]):
    """Persistent chat data manager."""

    _datas: Dict[str, ChatData] = {}
    _global_user_memories: Dict[str, Dict[str, str]] = {}  # global 用户记忆: {user_id: {key: value}}
    _custom_nicknames: Dict[str, str] = {}  # 用户自定义昵称: {user_id: nickname}
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
        if isinstance(raw_datas, dict):
            raw_global = raw_datas.pop("__global_user_memories", None)
            if isinstance(raw_global, dict):
                self._global_user_memories = raw_global
            else:
                self._global_user_memories = {}
            raw_nicknames = raw_datas.pop("__custom_nicknames", None)
            if isinstance(raw_nicknames, dict):
                self._custom_nicknames = {str(k): str(v) for k, v in raw_nicknames.items() if v}
            else:
                self._custom_nicknames = {}
            self._datas = {
                k: ChatData._load_from_dict(v.__dict__ if isinstance(v, ChatData) else v)
                for k, v in raw_datas.items()
                if isinstance(v, (dict, ChatData))
            }
        else:
            self._global_user_memories = {}
            self._datas = {}
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
        raw_global = data.pop("__global_user_memories", None)
        if isinstance(raw_global, dict):
            self._global_user_memories = {
                str(uid): {str(k): str(v) for k, v in mems.items() if v}
                for uid, mems in raw_global.items()
                if isinstance(mems, dict)
            }
        else:
            self._global_user_memories = {}
        raw_nicknames = data.pop("__custom_nicknames", None)
        if isinstance(raw_nicknames, dict):
            self._custom_nicknames = {str(k): str(v) for k, v in raw_nicknames.items() if v}
        else:
            self._custom_nicknames = {}
        self._datas = {
            k: ChatData._load_from_dict(v)
            for k, v in data.items()
            if isinstance(v, dict)
        }
        logger.info("读取历史数据成功")

    def _migrate_user_memories_to_global(self):
        """迁移旧数据：将各人格下的用户记忆合并到全局用户记忆空间。"""
        migrated_count = 0
        for chat_key, chat_data in self._datas.items():
            for preset_key, preset in chat_data.preset_datas.items():
                if not preset.user_memories:
                    continue
                for uid, memories in preset.user_memories.items():
                    if not memories:
                        continue
                    target = self._global_user_memories.setdefault(uid, {})
                    for k, v in memories.items():
                        if k not in target and v:
                            target[k] = v
                            migrated_count += 1
                # 迁移后清空人格下的用户记忆
                preset.user_memories.clear()
        if migrated_count > 0:
            logger.info(f"已迁移 {migrated_count} 条用户记忆到全局空间")

    def load_from_file(self):
        self._inited = False
        self._datas = {}
        if not self._compatibility_load():
            if config.NG_DATA_PICKLE:
                self._load_from_file_pickle()
            else:
                self._load_from_file_json()
        self._migrate_user_memories_to_global()
        self._last_save_data_time = 0
        self._inited = True

    @property
    def is_inited(self) -> bool:
        return self._inited

    def _save_to_file_pickle(self):
        save_data = dict(self._datas)
        if self._global_user_memories:
            save_data["__global_user_memories"] = self._global_user_memories
        if self._custom_nicknames:
            save_data["__custom_nicknames"] = self._custom_nicknames
        # 原子写入：先写临时文件，再 replace 覆盖
        tmp_path = self._file_path + ".tmp"
        with open(tmp_path, "wb") as f:
            pickle.dump(save_data, f)
        try:
            os.replace(tmp_path, self._file_path)
        except OSError:
            # fallback：直接写入（目标文件被锁定等场景）
            with open(self._file_path, "wb") as f:
                pickle.dump(save_data, f)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _save_to_file_json(self):
        save_data = dict(self._datas)
        if self._global_user_memories:
            save_data["__global_user_memories"] = self._global_user_memories
        if self._custom_nicknames:
            save_data["__custom_nicknames"] = self._custom_nicknames
        # 原子写入：先写临时文件，再 replace 覆盖
        tmp_path = self._file_path + ".tmp"
        with open(tmp_path, mode="w", encoding="utf-8") as fw:
            json.dump(save_data, fw, ensure_ascii=False, sort_keys=True, indent=2, cls=StoreEncoder)
        try:
            os.replace(tmp_path, self._file_path)
        except OSError:
            # fallback：直接写入（目标文件被锁定等场景）
            with open(self._file_path, mode="w", encoding="utf-8") as fw:
                json.dump(save_data, fw, ensure_ascii=False, sort_keys=True, indent=2, cls=StoreEncoder)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _dump_to_file(self):
        """实际的序列化+写盘（可在工作线程执行）。失败仅告警，由下一次保存兜底；
        原子写（.tmp + os.replace）保证失败不会损坏已有数据文件。"""
        with _SAVE_LOCK:
            try:
                if config.NG_DATA_PICKLE:
                    self._save_to_file_pickle()
                else:
                    self._save_to_file_json()
            except Exception as e:
                # 序列化期间数据被并发修改（如 remember 写入记忆字典）会导致失败，
                # 属可恢复场景：文件未被破坏，下次保存重写即可
                logger.warning(f"数据保存失败（下次保存自动重试）: {e!r}")
                return
        logger.info("数据保存成功")

    def save_to_file(self, must_save: bool = False):
        """节流持久化。事件循环运行中时把序列化+写盘卸载到执行器线程，避免大 JSON
        dump 阻塞 bot 响应；无运行中循环（启动迁移等场景）退化为同步写。"""
        if not must_save and time.time() - self._last_save_data_time < 60:
            return
        self._last_save_data_time = time.time()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._dump_to_file()
        else:
            loop.run_in_executor(None, self._dump_to_file)

    def save_to_file_blocking(self):
        """同步保存并等待完成，仅 shutdown 钩子使用，保证进程退出前落盘。"""
        self._last_save_data_time = time.time()
        self._dump_to_file()

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

    def get_global_user_memories(self, user_id: str) -> Dict[str, str]:
        """获取指定用户的 global 记忆。不存在时自动创建。"""
        uid = str(user_id)
        if uid not in self._global_user_memories:
            self._global_user_memories[uid] = {}
        return self._global_user_memories[uid]

    def set_global_user_memories(self, user_id: str, memories: Dict[str, str]) -> None:
        """设置指定用户的 global 记忆。"""
        self._global_user_memories[str(user_id)] = memories

    def get_custom_nickname(self, user_id: str) -> str:
        """获取用户自定义昵称，不存在时返回空字符串。"""
        return self._custom_nicknames.get(str(user_id), "")

    def set_custom_nickname(self, user_id: str, nickname: str) -> None:
        """设置用户自定义昵称。传空字符串表示清除。"""
        uid = str(user_id)
        if nickname:
            self._custom_nicknames[uid] = nickname
        else:
            self._custom_nicknames.pop(uid, None)

    def init_global_memory(self, chat_key: str) -> str:
        """为指定会话开启 global 群记忆，合并该会话所有人格的群记忆到 global 空间。返回合并报告。"""
        chat_data = self._datas.get(chat_key)
        if not chat_data:
            return "会话不存在。"

        chat_data.global_memory_enabled = True

        # 合并该会话所有人格的群记忆
        merged_group: Dict[str, str] = dict(chat_data.global_chat_memory)
        group_parts = []
        for preset_key, preset in chat_data.preset_datas.items():
            if preset.chat_memory:
                group_parts.append(f"{preset_key}: {len(preset.chat_memory)}条")
                for k, v in preset.chat_memory.items():
                    if k not in merged_group:
                        merged_group[k] = v
                preset.chat_memory.clear()
        chat_data.global_chat_memory = merged_group

        report_parts = []
        if group_parts:
            report_parts.append(f"群记忆 <- {', '.join(group_parts)}")
        else:
            report_parts.append("群记忆: 无合并")
        report_parts.append("用户记忆: 固定全群全人格共享")

        return "\n".join(report_parts)


@driver.on_shutdown
async def _():
    logger.info("正在保存数据，完成前请勿强制结束")
    PersistentDataManager.instance.save_to_file_blocking()
    logger.info("保存完成")
