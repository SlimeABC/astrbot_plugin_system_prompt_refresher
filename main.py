"""astrbot_plugin_system_prompt_refresher（系统提示词刷新）

管理员在对话中 @机器人或使用唤醒词后发送 ``refresh_prompt confirm``，
插件会在完整保留当前对话上下文的前提下刷新系统提示词：

1. 借本轮请求拿到 AstrBot 刚刚重新构建出的完整系统提示词原文；
2. 从当前对话历史中移除上一次刷新留下的快照；
3. 把最新原文写入历史末尾，作为一条明确标记为“非用户输入”的高优先级提醒；
4. 默认先把刷新前的完整对话另存为备份（标题为“原标题_backup”），再切回原对话；
5. 在原对话写入带快照的新历史，后续继续在原对话中对话；
6. 阻止本轮 LLM 调用，直接回复刷新结果。

刷新后会继续使用原对话。模型会在距离生成位置最近的上下文里重新读到
完整系统提示词，从而尽量摆脱长上下文形成的习惯与偏移。系统提示词不会被
模型改写或总结，因此刷新结果不受当前对话内容影响。

另提供 ``copy_chat confirm``：只把当前对话另存为备份（原标题_backup），
继续在原对话中，不注入系统提示词、不调用模型。

作者：Slime
版本：1.0.0
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:  # 兼容未导出 AstrBotConfig 的旧版本
    from astrbot.api import AstrBotConfig
except ImportError:  # pragma: no cover
    AstrBotConfig = dict

try:  # AstrBot 4.x 的公开导出
    from astrbot.api.event import MessageChain
except ImportError:  # pragma: no cover - 兼容旧版本
    from astrbot.core.message.message_event_result import MessageChain

try:  # WebUI API；旧版本没有时退化为纯对话插件
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover
    error_response = json_response = None
    request = None


PLUGIN_NAME = "astrbot_plugin_system_prompt_refresher"
PLUGIN_VERSION = "1.0.0"
PLUGIN_AUTHOR = "Slime"
PLUGIN_DESC = "在长对话中使模型重新遵循系统提示词，或修改提示词后刷新使其生效，并保留对话上下文。"

DEFAULT_COMMAND = "refresh_prompt"
DEFAULT_CONFIRM = "confirm"
DEFAULT_CONFIRM_REQUIRED_TEXT = "请添加confirm确认"
DEFAULT_SUCCESS_TEXT = "系统提示词已刷新，对话上下文已保留"
DEFAULT_BACKUP_SUCCESS_TEXT = "系统提示词已刷新，已另存备份，继续在原对话中"
DEFAULT_ADMIN_ONLY_TEXT = "仅管理员可以执行此操作"
DEFAULT_BACKUP_TITLE = "{title}_backup"
DEFAULT_COPY_COMMAND = "copy_chat"
DEFAULT_COPY_SUCCESS_TEXT = "已另存当前对话备份，继续在原对话中"
DEFAULT_DELETE_SUCCESS_TEXT = "对话已删除"

# 用于识别插件自己写入的快照，避免刷新时把普通用户消息误删。
REFRESH_MARKER = "<!-- ASTRBOT_SYSTEM_PROMPT_REFRESH -->"

MAX_CONVERSATIONS = 500
MAX_PROMPT_CACHE = 500
MAX_SESSION_NAME_LOOKUPS = 40
SESSION_NAME_LOOKUP_TIMEOUT = 2.5
SESSION_NAME_FAIL_TTL = 120
MAX_SESSION_NAME_CACHE = 500

# 系统提示词缓存持久化到插件 KV，插件重载/重启后仍可刷新。
PROMPT_CACHE_STORE_KEY = "prompt_cache_v1"
MAX_PERSISTED_PROMPTS = 20
MAX_PERSISTED_CHARS = 300_000
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

PREFERENCE_DEFAULTS = {
    "query": "",
    "bots": [],
    "types": [],
    "sort": "updated",
    "direction": "desc",
    "group_by_session": False,
}
_SORT_VALUES = {"updated", "messages", "chars", "title", "created"}
_CHAT_TYPE_VALUES = {"private", "group"}

PLUGIN_FEATURES = [
    "prompt_cache",
    "prompt_cache_snapshot_fallback",
    "persona_edit",
    "preferences",
    "cache_ttl",
    "auto_refresh",
]

# 允许 /refresh_prompt、！refresh_prompt 等写法。
_LEADING_PREFIX_RE = re.compile(r"^[\s/／!！.。]+")


def _flatten_config(value: Any) -> dict[str, Any]:
    """把 WebUI 的分组配置展平，兼容旧版扁平配置。"""
    result: dict[str, Any] = {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        if isinstance(item, dict):
            result.update(_flatten_config(item))
        else:
            result[str(key)] = item
    return result


async def _maybe_await(value: Any) -> Any:
    """兼容同步 / 异步的对话管理器实现。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _message_text(message: Any) -> str:
    """读取历史消息的纯文本内容，兼容字符串与多模态列表。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif isinstance(part, str):
                chunks.append(part)
        return "\n".join(chunks)
    return ""


def _is_refresh_snapshot(message: Any) -> bool:
    """判断一条历史消息是否为插件写入的系统提示词快照。"""
    return REFRESH_MARKER in _message_text(message)


def _build_snapshot(system_prompt: str, max_chars: int = 0) -> str:
    """构造写入对话历史的系统提示词快照。"""
    original = system_prompt
    truncated = False
    if max_chars > 0 and len(original) > max_chars:
        original = original[:max_chars].rstrip()
        truncated = True

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        REFRESH_MARKER,
        "【系统提示词刷新 · 非用户输入 · 管理员操作】",
        f"刷新时间：{now}",
        "管理员刚刚主动刷新了系统提示词。以下是从当前 AstrBot 配置、人格与插件注入中重新读取到的完整系统提示词原文。",
        "它优先于此前对话中形成的习惯、默认做法和偏移；请在后续回复中重新以它为准，并且不要向用户复述、引用或解释这段内容。",
        "",
        "----- 系统提示词原文开始 -----",
        original,
        "----- 系统提示词原文结束 -----",
    ]
    if truncated:
        lines.append(f"（原文超过 {max_chars} 字符，已截断。）")
    lines.append("（这是插件写入的上下文提醒，不是用户发言。请直接继续正常回应用户。）")
    return "\n".join(lines)


class _WebEvent:
    """WebUI 操作时使用的轻量事件对象。

    不提供 send / plain_result，确保 WebUI 操作只修改对话数据，
    不会向聊天平台发送任何机器人消息。
    """

    def __init__(self, umo: str, platform_id: str = ""):
        self.unified_msg_origin = umo
        self._platform_id = platform_id

    def get_platform_id(self):
        return self._platform_id or None


class _WebRequest:
    """WebUI 刷新时传给 _perform_refresh 的轻量请求对象。"""

    def __init__(self, system_prompt: str, conversation: Any):
        self.system_prompt = system_prompt
        self.conversation = conversation


class SystemPromptRefresher(Star):
    """对话内刷新 LLM 系统提示词，同时保留全部对话上下文。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._prompt_cache: dict[str, dict[str, Any]] = {}
        self._umo_prompt_cache: dict[str, dict[str, Any]] = {}
        self._cid_prompt_cache: dict[str, dict[str, Any]] = {}
        self._session_name_cache: dict[str, str] = {}
        self._session_name_fail_at: dict[str, float] = {}
        self._conversations_cache: tuple[float, dict[str, Any]] | None = None
        self._prompt_cache_loaded = False
        self._prompt_cache_save_task: asyncio.Task | None = None
        self._register_web_apis(context)
        logger.info(f"{PLUGIN_NAME} v{PLUGIN_VERSION} 已加载")

    def _register_web_apis(self, context: Context | None) -> None:
        """注册 WebUI 使用的插件 API。"""
        if context is None or json_response is None:
            return
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.warning(f"[{PLUGIN_NAME}] 当前 AstrBot 版本不支持 WebUI 插件 API")
            return
        routes = [
            (
                f"/{PLUGIN_NAME}/conversations",
                self.api_conversations,
                ["GET"],
                "对话列表与刷新状态",
            ),
            (
                f"/{PLUGIN_NAME}/preferences",
                self.api_preferences,
                ["GET", "POST"],
                "WebUI 筛选与偏好",
            ),
            (
                f"/{PLUGIN_NAME}/prompt",
                self.api_prompt,
                ["GET"],
                "已记录的系统提示词与人格片段",
            ),
            (
                f"/{PLUGIN_NAME}/refresh",
                self.api_refresh,
                ["POST"],
                "刷新系统提示词",
            ),
            (
                f"/{PLUGIN_NAME}/copy",
                self.api_copy,
                ["POST"],
                "另存对话备份",
            ),
            (
                f"/{PLUGIN_NAME}/clear-snapshot",
                self.api_clear_snapshot,
                ["POST"],
                "清除系统提示词快照",
            ),
            (
                f"/{PLUGIN_NAME}/delete-conversation",
                self.api_delete_conversation,
                ["POST"],
                "删除对话",
            ),
        ]
        for route, handler, methods, desc in routes:
            try:
                register(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[{PLUGIN_NAME}] 注册 Web API {route} 失败: {exc!r}"
                )

    def _invalidate_conversations_cache(self) -> None:
        """对话数据发生变化后清空 WebUI 列表缓存。"""
        self._conversations_cache = None

    def _preferences_key(self, username: str) -> str:
        return f"ui_preferences_{username or 'default'}"

    @staticmethod
    def _sanitize_preferences(value: Any) -> dict[str, Any]:
        data = value if isinstance(value, dict) else {}
        query = str(data.get("query") or "").strip()[:200]

        bots: list[str] = []
        if isinstance(data.get("bots"), list):
            for item in data["bots"][:50]:
                text = str(item or "").strip()[:200]
                if text and text not in bots:
                    bots.append(text)

        types: list[str] = []
        if isinstance(data.get("types"), list):
            for item in data["types"]:
                text = str(item or "").strip().lower()
                if text in _CHAT_TYPE_VALUES and text not in types:
                    types.append(text)

        sort = str(data.get("sort") or "updated").strip().lower()
        if sort not in _SORT_VALUES:
            sort = "updated"

        direction = str(data.get("direction") or "desc").strip().lower()
        if direction not in ("asc", "desc"):
            direction = "desc"

        group = data.get("group_by_session")
        if isinstance(group, str):
            group = group.strip().lower() in ("1", "true", "yes", "on")
        else:
            group = bool(group)

        return {
            "query": query,
            "bots": bots,
            "types": types,
            "sort": sort,
            "direction": direction,
            "group_by_session": group,
        }

    async def _load_preferences(self, username: str) -> dict[str, Any]:
        getter = getattr(self, "get_kv_data", None)
        if callable(getter):
            try:
                value = await _maybe_await(
                    getter(
                        self._preferences_key(username),
                        dict(PREFERENCE_DEFAULTS),
                    )
                )
                return self._sanitize_preferences(value)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 读取 WebUI 偏好失败: {exc!r}")
        return dict(PREFERENCE_DEFAULTS)

    async def _save_preferences(
        self, username: str, prefs: dict[str, Any]
    ) -> None:
        setter = getattr(self, "put_kv_data", None)
        if not callable(setter):
            return
        try:
            await _maybe_await(
                setter(self._preferences_key(username), dict(prefs))
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 保存 WebUI 偏好失败: {exc!r}")

    async def api_preferences(self):
        """读取/保存当前 Dashboard 用户的 WebUI 筛选与排序偏好。"""
        if json_response is None or error_response is None:
            return None
        username = str(getattr(request, "username", "") or "default")
        method = str(getattr(request, "method", "") or "").upper()
        if method == "POST":
            payload = await self._json_body()
            prefs = self._sanitize_preferences(payload)
            await self._save_preferences(username, prefs)
            return json_response({"saved": True, **prefs})
        return json_response(await self._load_preferences(username))

    # -- 配置读取 ---------------------------------------------------------

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            return _flatten_config(self.config).get(key, default)
        except Exception:  # noqa: BLE001 - 配置异常不应打断消息管道
            return default

    def _str(self, key: str, default: str = "") -> str:
        value = self._get(key, default)
        text = str(value if value is not None else default).strip()
        return text or default

    def _bool(self, key: str, default: bool = False) -> bool:
        value = self._get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _int(self, key: str, default: int, minimum: int = 0) -> int:
        try:
            value = int(self._get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _command_name(self) -> str:
        return self._str("command_name", DEFAULT_COMMAND).lower()

    def _confirm_word(self) -> str:
        return self._str("confirm_word", DEFAULT_CONFIRM).lower()

    def _copy_command_name(self) -> str:
        return self._str("copy_command_name", DEFAULT_COPY_COMMAND).lower()

    def _reply_text(self, text: Any) -> str:
        """按配置去掉发送给用户的话末尾的句号。"""
        value = str(text or "")
        if not self._bool("strip_trailing_period", True):
            return value
        return value.rstrip().rstrip("。").rstrip()

    @staticmethod
    def _cache_key(umo: str, cid: str) -> str:
        return f"{umo}|{cid}"

    def _cache_prompt(
        self,
        umo: Any,
        cid: Any,
        prompt: Any,
        persona_prompt: Any = None,
    ) -> None:
        """缓存对话最近一次请求的系统提示词，供 WebUI 刷新使用。

        只要拿到系统提示词就缓存：cid 为空时按 umo 兜底（旧版 AstrBot 的
        ProviderRequest 没有 conversation 字段），cid 非空时额外按 cid 建索引，
        保证 WebUI 三种查法都能命中。
        """
        umo_text = str(umo or "").strip()
        cid_text = str(cid or "").strip()
        prompt_text = str(prompt or "").strip()
        if not prompt_text or not (umo_text or cid_text):
            logger.debug(
                f"[{PLUGIN_NAME}] 跳过缓存系统提示词："
                f"umo={umo_text!r} cid={cid_text!r} chars={len(prompt_text)}"
            )
            return
        # 人格片段一律从完整提示词里提取，保证与提示词里的原文一致。
        # 传入的 persona_prompt 可能来自 AstrBot 人格配置（含未渲染占位符），
        # 与提示词中渲染后的文本不一致，会导致 WebUI 刷新时报“人格片段已变化”。
        persona_text = self._extract_persona_prompt(prompt_text)
        entry = {
            "prompt": prompt_text,
            "chars": len(prompt_text),
            "updated_at": int(time.time()),
            "persona_prompt": persona_text,
            "umo": umo_text,
            "cid": cid_text,
        }
        existing = None
        if cid_text:
            existing = self._prompt_cache.get(self._cache_key(umo_text, cid_text))
        if existing is None and umo_text:
            existing = self._umo_prompt_cache.get(umo_text)
        if cid_text:
            self._prompt_cache[self._cache_key(umo_text, cid_text)] = entry
            self._cid_prompt_cache[cid_text] = entry
        if umo_text:
            self._umo_prompt_cache[umo_text] = entry
        logger.debug(
            f"[{PLUGIN_NAME}] 已记录系统提示词：umo={umo_text} cid={cid_text} "
            f"chars={len(prompt_text)}"
        )
        if existing is None:
            # 首次记录用 info，方便在 AstrBot 日志里确认插件确实收到了请求。
            logger.info(
                f"[{PLUGIN_NAME}] 已记录系统提示词：umo={umo_text} "
                f"cid={cid_text or '-'} chars={len(prompt_text)}"
            )
        if existing is None:
            # 列表缓存里的 can_refresh 是写入时算出的，首次记录必须失效，
            # 否则 WebUI 会一直显示“尚未记录系统提示词”。
            self._invalidate_conversations_cache()
        unchanged = bool(
            existing
            and existing.get("prompt") == prompt_text
            and existing.get("persona_prompt") == persona_text
        )
        if not unchanged:
            self._schedule_prompt_cache_save()
        if len(self._prompt_cache) > MAX_PROMPT_CACHE:
            oldest = min(
                self._prompt_cache,
                key=lambda key: self._prompt_cache[key]["updated_at"],
            )
            self._prompt_cache.pop(oldest, None)
        if len(self._umo_prompt_cache) > MAX_PROMPT_CACHE:
            oldest_umo = min(
                self._umo_prompt_cache,
                key=lambda key: self._umo_prompt_cache[key]["updated_at"],
            )
            self._umo_prompt_cache.pop(oldest_umo, None)

    def _prompt_entry(self, umo: Any, cid: Any) -> dict[str, Any] | None:
        """按 umo|cid → cid → umo 三级查找已记录的系统提示词。"""
        umo_text = str(umo or "").strip()
        cid_text = str(cid or "").strip()
        if umo_text and cid_text:
            entry = self._prompt_cache.get(self._cache_key(umo_text, cid_text))
            if entry:
                return entry
        if cid_text:
            entry = self._cid_prompt_cache.get(cid_text)
            if entry:
                return entry
        if umo_text:
            entry = self._umo_prompt_cache.get(umo_text)
            if entry:
                return entry
        return None

    def _schedule_prompt_cache_save(self) -> None:
        """把系统提示词缓存异步写回插件 KV，失败不影响主流程。"""
        if not callable(getattr(self, "get_kv_data", None)):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._prompt_cache_save_task and not self._prompt_cache_save_task.done():
            return
        try:
            self._prompt_cache_save_task = loop.create_task(
                self._save_prompt_cache()
            )
        except Exception:  # noqa: BLE001
            self._prompt_cache_save_task = None

    async def _save_prompt_cache(self) -> None:
        setter = getattr(self, "put_kv_data", None)
        if not callable(setter):
            return
        entries: list[dict[str, Any]] = []
        total_chars = 0
        ordered = sorted(
            self._umo_prompt_cache.values(),
            key=lambda entry: int(entry.get("updated_at") or 0),
            reverse=True,
        )
        for entry in ordered:
            if len(entries) >= MAX_PERSISTED_PROMPTS:
                break
            chars = int(entry.get("chars") or len(str(entry.get("prompt") or "")))
            if entries and total_chars + chars > MAX_PERSISTED_CHARS:
                break
            total_chars += chars
            entries.append(dict(entry))
        try:
            await _maybe_await(setter(PROMPT_CACHE_STORE_KEY, entries))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 保存系统提示词缓存失败: {exc!r}")

    async def _load_prompt_cache(self) -> None:
        """从插件 KV 恢复系统提示词缓存，让插件重载后仍可刷新。"""
        if self._prompt_cache_loaded:
            return
        self._prompt_cache_loaded = True
        getter = getattr(self, "get_kv_data", None)
        if not callable(getter):
            return
        try:
            stored = await _maybe_await(getter(PROMPT_CACHE_STORE_KEY, []))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 读取系统提示词缓存失败: {exc!r}")
            return
        if not isinstance(stored, list):
            return
        for entry in stored:
            if not isinstance(entry, dict):
                continue
            prompt_text = str(entry.get("prompt") or "").strip()
            if not prompt_text:
                continue
            item = {
                "prompt": prompt_text,
                "chars": int(entry.get("chars") or len(prompt_text)),
                "updated_at": int(entry.get("updated_at") or 0),
                "persona_prompt": str(entry.get("persona_prompt") or ""),
                "umo": str(entry.get("umo") or ""),
                "cid": str(entry.get("cid") or ""),
            }
            umo_text = item["umo"]
            cid_text = item["cid"]
            if cid_text:
                self._prompt_cache[self._cache_key(umo_text, cid_text)] = item
                self._cid_prompt_cache[cid_text] = item
            if umo_text:
                self._umo_prompt_cache[umo_text] = item
        if stored:
            # 恢复出来的记录会让列表里的 can_refresh 变化，旧列表缓存必须失效。
            self._invalidate_conversations_cache()
            logger.info(
                f"[{PLUGIN_NAME}] 已从插件存储恢复 "
                f"{len(self._umo_prompt_cache)} 条系统提示词记录"
            )

    def _theme_color(self) -> str:
        value = self._str("theme_color", "#22c55e")
        if not _COLOR_RE.match(value):
            return "#22c55e"
        if len(value) == 4:
            value = "#" + "".join(char * 2 for char in value[1:])
        return value.lower()

    @staticmethod
    def _extract_persona_prompt(prompt: str) -> str:
        """从完整系统提示词中提取人格提示词片段。"""
        marker = "# Persona Instructions"
        start = prompt.find(marker)
        if start < 0:
            return ""
        content_start = start + len(marker)
        while content_start < len(prompt) and prompt[content_start] in " \t\r\n":
            content_start += 1
        next_heading = prompt.find("\n# ", content_start)
        next_subheading = prompt.find("\n## ", content_start)
        candidates = [
            position
            for position in (next_heading, next_subheading)
            if position >= 0
        ]
        end = min(candidates) if candidates else len(prompt)
        return prompt[content_start:end].strip()

    @staticmethod
    def _replace_persona_prompt(
        prompt: str, old_persona: str, new_persona: str
    ) -> str | None:
        """只替换人格段落中的人格文本，找不到时返回 None。"""
        if not old_persona:
            return None
        marker = "# Persona Instructions"
        marker_pos = prompt.find(marker)
        if marker_pos < 0:
            return None
        start = marker_pos + len(marker)
        next_heading = prompt.find("\n# ", start)
        next_subheading = prompt.find("\n## ", start)
        candidates = [
            position
            for position in (next_heading, next_subheading)
            if position >= 0
        ]
        end = min(candidates) if candidates else len(prompt)
        index = prompt.find(old_persona, start, end)
        if index < 0:
            return None
        return (
            prompt[:index]
            + new_persona
            + prompt[index + len(old_persona) :]
        )

    def _apply_prompt_override(
        self,
        umo: str,
        cid: str,
        prompt: str,
        persona_prompt: Any = None,
    ) -> None:
        """把本次 WebUI 修改后的人格片段更新到内存记录中。"""
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return
        persona_text = self._extract_persona_prompt(prompt_text)
        entry = {
            "prompt": prompt_text,
            "chars": len(prompt_text),
            "updated_at": int(time.time()),
            "persona_prompt": persona_text,
            "umo": str(umo or ""),
            "cid": str(cid or ""),
        }
        if cid:
            self._prompt_cache[self._cache_key(umo, cid)] = entry
            self._cid_prompt_cache[str(cid)] = entry
        if umo:
            self._umo_prompt_cache[str(umo)] = entry
        self._invalidate_conversations_cache()
        self._schedule_prompt_cache_save()

    # -- 指令解析与权限 ---------------------------------------------------

    def _parse_named(
        self, text: Any, command: str, confirm_word: str
    ) -> tuple[bool, bool]:
        """解析 <命令> [确认词]。

        Returns:
            (是否匹配指令, 是否带确认词)
        """
        cleaned = _LEADING_PREFIX_RE.sub("", str(text or ""))
        cleaned = re.sub(r"[，,；;：:]+", " ", cleaned).strip()
        parts = cleaned.split()
        if not parts:
            return False, False

        if not command or parts[0].lower() != command.lower():
            return False, False

        confirmed = len(parts) >= 2 and parts[1].lower() == confirm_word.lower()
        return True, confirmed

    def _parse(self, text: Any) -> tuple[bool, bool]:
        """解析 refresh_prompt [confirm]。"""
        return self._parse_named(text, self._command_name(), self._confirm_word())

    def _parse_copy(self, text: Any) -> tuple[bool, bool]:
        """解析 copy_chat [confirm]。"""
        return self._parse_named(text, self._copy_command_name(), self._confirm_word())

    @staticmethod
    def _is_woken(event: AstrMessageEvent) -> bool:
        """确认消息已通过 @、唤醒词或私聊唤醒。"""
        flag = getattr(event, "is_at_or_wake_command", None)
        if flag:
            return True
        checker = getattr(event, "is_private_chat", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # noqa: BLE001
                return False
        return flag is None

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """AstrBot 管理员判定，兼容旧版事件对象。"""
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                if checker():
                    return True
            except Exception:  # noqa: BLE001
                pass
        return str(getattr(event, "role", "") or "").strip().lower() == "admin"

    # -- 事件入口 ---------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def refresh_prompt_command(self, event: AstrMessageEvent):
        """处理 refresh_prompt 与 refresh_prompt confirm。

        无 confirm 时直接回复提示并终止事件，不调用 LLM；
        带 confirm 时放行到 on_llm_request，借本轮已构建好的系统提示词完成刷新。
        """
        if not self._is_woken(event):
            return

        matched, confirmed = self._parse(event.get_message_str())
        if not matched:
            return

        if self._bool("require_admin", True) and not self._is_admin(event):
            yield event.plain_result(
                self._reply_text(
                    self._str("reply_admin_only", DEFAULT_ADMIN_ONLY_TEXT)
                )
            )
            event.stop_event()
            return

        if not confirmed:
            yield event.plain_result(
                self._reply_text(
                    self._str(
                        "reply_confirm_required", DEFAULT_CONFIRM_REQUIRED_TEXT
                    )
                )
            )
            event.stop_event()
            return

        # 已确认：不在这里回复，也不终止事件，让本轮 LLM 请求正常构建。
        return

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def copy_chat_command(self, event: AstrMessageEvent):
        """处理 copy_chat 与 copy_chat confirm。

        把当前对话另存为备份，继续在原对话中；不注入系统提示词，也不调用 LLM。
        """
        if not self._is_woken(event):
            return

        matched, confirmed = self._parse_copy(event.get_message_str())
        if not matched:
            return

        if self._bool("copy_require_admin", True) and not self._is_admin(event):
            yield event.plain_result(
                self._reply_text(
                    self._str("reply_admin_only", DEFAULT_ADMIN_ONLY_TEXT)
                )
            )
            event.stop_event()
            return

        if not confirmed:
            yield event.plain_result(
                self._reply_text(
                    self._str(
                        "reply_confirm_required", DEFAULT_CONFIRM_REQUIRED_TEXT
                    )
                )
            )
            event.stop_event()
            return

        try:
            success, reply = await self._copy_current_conversation(event)
        except Exception as exc:  # noqa: BLE001 - 备份失败不应打断消息管道
            logger.error(f"[{PLUGIN_NAME}] 备份当前对话失败: {exc!r}")
            success, reply = False, f"备份当前对话失败：{exc}"

        yield event.plain_result(self._reply_text(reply))
        event.stop_event()
        logger.info(
            f"[{PLUGIN_NAME}] 备份当前对话{'成功' if success else '失败'}：{reply}"
        )

    @filter.on_llm_request()
    async def refresh_system_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在本轮请求中取得完整系统提示词并写入对话历史。"""
        await self._load_prompt_cache()
        # 先缓存系统提示词：这一步与本轮是否唤醒、是否刷新无关，
        # 只要发生了 LLM 请求就记录，WebUI 才能拿到可刷新的提示词。
        cached_conversation = getattr(req, "conversation", None)
        cached_cid = (
            str(getattr(cached_conversation, "cid", "") or "")
            if cached_conversation
            else ""
        )
        system_prompt_text = str(getattr(req, "system_prompt", "") or "")
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not cached_cid and (umo or system_prompt_text):
            manager = getattr(self.context, "conversation_manager", None)
            cached_cid = await self._current_cid(manager, umo)
        self._cache_prompt(umo, cached_cid, system_prompt_text)

        matched, confirmed = self._parse(event.get_message_str())
        if not matched or not confirmed:
            return

        # 只有刷新命令需要唤醒判断；上面的提示词记录不受影响。
        if not self._is_woken(event):
            return

        if self._bool("require_admin", True) and not self._is_admin(event):
            return

        try:
            success, reply = await self._perform_refresh(event, req)
        except Exception as exc:  # noqa: BLE001 - 刷新失败不应打断消息管道
            logger.error(f"[{PLUGIN_NAME}] 刷新系统提示词失败: {exc!r}")
            success, reply = False, f"刷新系统提示词失败：{exc}"

        try:
            await event.send(MessageChain().message(self._reply_text(reply)))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 发送刷新结果失败: {exc!r}")

        # 阻止本轮 LLM 调用：刷新结果由插件直接回复。
        event.stop_event()
        logger.info(f"[{PLUGIN_NAME}] 刷新{'成功' if success else '失败'}：{reply}")

    # -- 刷新实现 ---------------------------------------------------------

    async def _perform_refresh(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> tuple[bool, str]:
        """把最新系统提示词快照写入当前对话末尾，不删除任何已有上下文。"""
        system_prompt = str(getattr(req, "system_prompt", "") or "").strip()
        if not system_prompt:
            return (
                False,
                "当前系统提示词为空，无法刷新。请检查人格或系统提示词配置后再试",
            )

        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return False, "当前 AstrBot 版本不支持读取对话历史，无法刷新"

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        conversation = getattr(req, "conversation", None)
        cid = str(getattr(conversation, "cid", "") or "") if conversation else ""

        if not cid and umo:
            getter = getattr(manager, "get_curr_conversation_id", None)
            if callable(getter):
                cid = str(await _maybe_await(getter(umo)) or "")

        if not cid:
            return False, "当前会话还没有对话记录，请先正常聊一轮后再刷新"

        if conversation is None:
            getter = getattr(manager, "get_conversation", None)
            if callable(getter):
                try:
                    conversation = await _maybe_await(getter(umo, cid))
                except TypeError:
                    conversation = await _maybe_await(getter(umo, cid, False))

        if conversation is None:
            return False, "读取当前对话失败，无法刷新"

        raw_history = getattr(conversation, "history", "") or "[]"
        if isinstance(raw_history, (bytes, bytearray)):
            raw_history = raw_history.decode("utf-8", errors="replace")
        try:
            history = (
                json.loads(raw_history)
                if isinstance(raw_history, str)
                else list(raw_history)
            )
        except Exception:  # noqa: BLE001
            return False, "当前对话历史解析失败，为避免破坏上下文，已取消刷新"

        if not isinstance(history, list):
            return False, "当前对话历史格式异常，为避免破坏上下文，已取消刷新"

        max_chars = self._int("max_chars", 0, 0)
        snapshot = _build_snapshot(system_prompt, max_chars)

        original_history = history
        if self._bool("replace_previous", True):
            refreshed_history = [
                item for item in original_history if not _is_refresh_snapshot(item)
            ]
        else:
            refreshed_history = list(original_history)
        refreshed_history.append({"role": "user", "content": snapshot})

        backup_cid = ""
        if self._bool("backup_before_refresh", True):
            backup_cid, error = await self._create_backup_conversation(
                manager=manager,
                event=event,
                umo=umo,
                conversation=conversation,
                history=original_history,
                old_cid=cid,
            )
            if not backup_cid:
                return False, error

        updater = getattr(manager, "update_conversation", None)
        if not callable(updater):
            return False, "当前 AstrBot 版本不支持写入对话历史，无法刷新"

        try:
            await _maybe_await(updater(umo, cid, refreshed_history))
        except TypeError:  # pragma: no cover - 兼容不同版本的参数形式
            await _maybe_await(updater(umo, cid, history=refreshed_history))

        logger.info(
            f"[{PLUGIN_NAME}] 已写入系统提示词快照：umo={umo} cid={cid} "
            f"chars={len(system_prompt)} history={len(refreshed_history)} "
            f"backup_cid={backup_cid or 'none'}"
        )
        self._invalidate_conversations_cache()
        if backup_cid:
            return True, self._str(
                "reply_success_backup", DEFAULT_BACKUP_SUCCESS_TEXT
            )
        return True, self._str("reply_success", DEFAULT_SUCCESS_TEXT)

    async def _copy_current_conversation(
        self, event: AstrMessageEvent
    ) -> tuple[bool, str]:
        """把当前对话另存为备份，继续在原对话中。"""
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return False, "当前 AstrBot 版本不支持读取对话历史，无法备份"

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        cid = ""
        getter = getattr(manager, "get_curr_conversation_id", None)
        if umo and callable(getter):
            cid = str(await _maybe_await(getter(umo)) or "")
        if not cid:
            return False, "当前会话还没有对话记录，先正常聊几句再备份吧"

        conversation = None
        getter = getattr(manager, "get_conversation", None)
        if callable(getter):
            try:
                conversation = await _maybe_await(getter(umo, cid))
            except TypeError:
                conversation = await _maybe_await(getter(umo, cid, False))
        if conversation is None:
            return False, "读取当前对话失败，无法备份"

        raw_history = getattr(conversation, "history", "") or "[]"
        if isinstance(raw_history, (bytes, bytearray)):
            raw_history = raw_history.decode("utf-8", errors="replace")
        try:
            history = (
                json.loads(raw_history)
                if isinstance(raw_history, str)
                else list(raw_history)
            )
        except Exception:  # noqa: BLE001
            return False, "当前对话历史解析失败，无法备份"
        if not isinstance(history, list):
            return False, "当前对话历史格式异常，无法备份"

        backup_cid, error = await self._create_backup_conversation(
            manager=manager,
            event=event,
            umo=umo,
            conversation=conversation,
            history=history,
            old_cid=cid,
        )
        if not backup_cid:
            return False, error

        logger.info(
            f"[{PLUGIN_NAME}] 已另存当前对话备份：umo={umo} old_cid={cid} "
            f"backup_cid={backup_cid} history={len(history)}"
        )
        self._invalidate_conversations_cache()
        return True, self._str("reply_copy_success", DEFAULT_COPY_SUCCESS_TEXT)

    async def _create_backup_conversation(
        self,
        *,
        manager: Any,
        event: AstrMessageEvent,
        umo: str,
        conversation: Any,
        history: list[dict],
        old_cid: str,
        title_override: str | None = None,
    ) -> tuple[str | None, str]:
        """另存一份当前对话作为备份，并切回原对话。"""
        creator = getattr(manager, "new_conversation", None)
        switcher = getattr(manager, "switch_conversation", None)
        if not callable(creator) or not callable(switcher):
            return None, "当前 AstrBot 版本不支持备份对话，原对话未改动"

        title = (
            str(title_override).strip()[:255]
            if title_override
            else self._build_backup_title(conversation)
        )
        persona_id = getattr(conversation, "persona_id", None)

        platform_id = None
        platform_getter = getattr(event, "get_platform_id", None)
        if callable(platform_getter):
            try:
                platform_id = platform_getter()
            except Exception:  # noqa: BLE001
                platform_id = None

        try:
            backup_cid = await _maybe_await(
                creator(
                    umo,
                    platform_id=platform_id,
                    content=history,
                    title=title,
                    persona_id=persona_id,
                )
            )
        except TypeError:  # pragma: no cover - 兼容没有 platform_id 参数的旧版本
            backup_cid = await _maybe_await(
                creator(umo, content=history, title=title, persona_id=persona_id)
            )

        if not backup_cid:
            return None, "备份对话创建失败，原对话未改动"

        self._invalidate_conversations_cache()
        try:
            await _maybe_await(switcher(umo, old_cid))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 备份后切回原对话失败: {exc!r}")
            return None, f"备份已创建，但切回原对话失败：{exc}"

        return str(backup_cid), ""

    def _build_backup_title(self, conversation: Any) -> str:
        """生成 xxx_backup 形式的备份标题。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        raw_title = str(getattr(conversation, "title", "") or "").strip()
        if not raw_title:
            raw_title = f"对话_{now}"
        template = self._str("backup_conversation_title", DEFAULT_BACKUP_TITLE)
        title = template.replace("{title}", raw_title).replace("{time}", now)
        return title[:250]

    # -- WebUI API ---------------------------------------------------------

    async def _json_body(self) -> dict[str, Any]:
        if request is None:
            return {}
        try:
            payload = await request.json(default={})
        except TypeError:
            try:
                payload = await request.json()
            except Exception:  # noqa: BLE001
                return {}
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _split_umo(value: Any) -> tuple[str, str, str]:
        parts = str(value or "").split(":")
        if len(parts) >= 3:
            return parts[0], ":".join(parts[1:-1]), parts[-1]
        return "", "", str(value or "")

    @staticmethod
    def _chat_type(message_type: str) -> str:
        lower = str(message_type or "").lower()
        if "group" in lower:
            return "group"
        if "friend" in lower or "private" in lower or "direct" in lower:
            return "private"
        return "unknown"

    def _platform_meta(self) -> dict[str, dict[str, str]]:
        """收集已加载平台实例（机器人）的 ID、名称与类型。"""
        result: dict[str, dict[str, str]] = {}
        platform_manager = getattr(self.context, "platform_manager", None)
        if platform_manager is None:
            return result
        try:
            instances = platform_manager.get_insts()
        except Exception:  # noqa: BLE001
            return result
        for instance in instances or []:
            meta = None
            meta_getter = getattr(instance, "meta", None)
            if callable(meta_getter):
                try:
                    meta = meta_getter()
                except Exception:  # noqa: BLE001
                    meta = None
            config = getattr(instance, "config", None)
            if not isinstance(config, dict):
                config = {}
            platform_id = (
                self._read_meta(meta, "id")
                or getattr(instance, "platform_id", None)
                or getattr(instance, "id", None)
                or config.get("id")
            )
            if not isinstance(platform_id, str) or not platform_id:
                continue
            name = (
                config.get("name")
                or self._read_meta(meta, "name")
                or platform_id
            )
            platform_type = (
                self._read_meta(meta, "type")
                or getattr(instance, "type", None)
                or config.get("type")
                or ""
            )
            result[platform_id] = {
                "platform_id": platform_id,
                "name": str(name),
                "type": str(platform_type),
            }
        return result

    @staticmethod
    def _read_meta(meta: Any, key: str) -> Any:
        if isinstance(meta, dict):
            return meta.get(key)
        return getattr(meta, key, None)

    def _platform_client(self, platform_id: str) -> Any:
        """尽力获取平台适配器客户端，用于查询会话昵称。"""
        for client in self._platform_clients(platform_id):
            return client
        return None

    def _platform_clients(self, platform_id: str) -> list[Any]:
        """按实例 ID 取适配器客户端，找不到时按平台名/类型回退。

        AstrBot 里 `conversation.platform_id` 有时是实例 ID（如 `bot1`），
        有时是平台名（如 `aiocqhttp`），两种都要能命中，否则昵称查不到。
        """
        insts: list[Any] = []
        getter = getattr(self.context, "get_platform_inst", None)
        if callable(getter) and platform_id:
            try:
                inst = getter(platform_id)
            except Exception:  # noqa: BLE001
                inst = None
            if inst is not None:
                insts.append(inst)
        manager = getattr(self.context, "platform_manager", None)
        wanted = str(platform_id or "").strip().lower()
        for inst in getattr(manager, "platform_insts", None) or []:
            if any(inst is existing for existing in insts):
                continue
            meta = getattr(inst, "meta", None)
            meta_obj = meta() if callable(meta) else None
            keys = {
                str(self._read_meta(meta_obj, "id") or "").strip().lower(),
                str(self._read_meta(meta_obj, "name") or "").strip().lower(),
                str(self._read_meta(meta_obj, "type") or "").strip().lower(),
            }
            if wanted and wanted in keys:
                insts.append(inst)
        clients: list[Any] = []
        for inst in insts:
            client = self._resolve_platform_client(inst)
            if client is not None and all(
                client is not existing for existing in clients
            ):
                clients.append(client)
        return clients

    @staticmethod
    def _resolve_platform_client(inst: Any) -> Any:
        """从平台适配器实例上取出可调用 call_action 的客户端。"""
        for attribute in ("get_client", "client", "bot"):
            value = getattr(inst, attribute, None)
            if callable(value):
                try:
                    value = value()
                except Exception:  # noqa: BLE001
                    value = None
            if value is not None:
                return value
        return None

    @staticmethod
    def _to_int(value: str) -> int | str:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    async def _attach_session_names(self, items: list[dict[str, Any]]) -> None:
        """为每个会话补上昵称/群名，供 WebUI 显示“ID(名称)”。"""
        unique: dict[str, dict[str, Any]] = {}
        for item in items:
            umo = str(item.get("umo") or "")
            if umo and umo not in unique:
                unique[umo] = item
        pending = [
            item
            for umo, item in unique.items()
            if not self._session_name_cache.get(umo)
        ][:MAX_SESSION_NAME_LOOKUPS]
        if pending:
            semaphore = asyncio.Semaphore(5)

            async def fetch(item: dict[str, Any]) -> None:
                async with semaphore:
                    await self._lookup_session_name(
                        str(item.get("umo") or ""),
                        str(item.get("platform_id") or ""),
                        str(item.get("chat_type") or ""),
                        str(item.get("session_id") or ""),
                    )

            await asyncio.gather(*(fetch(item) for item in pending))
        for item in items:
            umo = str(item.get("umo") or "")
            item["session_name"] = self._session_name_cache.get(umo, "")

    async def _lookup_session_name(
        self, umo: str, platform_id: str, chat_type: str, session_id: str
    ) -> str:
        if not umo:
            return ""
        cached = self._session_name_cache.get(umo)
        if cached:
            return cached
        failed_at = self._session_name_fail_at.get(umo, 0.0)
        if failed_at and time.time() - failed_at < SESSION_NAME_FAIL_TTL:
            return ""

        name = ""
        if session_id and chat_type in ("group", "private"):
            if chat_type == "group":
                actions = (("get_group_info", "group_id"),)
            else:
                actions = (
                    ("get_stranger_info", "user_id"),
                    ("get_friend_info", "user_id"),
                )
            for client in self._platform_clients(platform_id):
                for action, param in actions:
                    result = await self._call_platform_action(
                        client, action, **{param: self._to_int(session_id)}
                    )
                    name = self._session_name_from_result(result, chat_type)
                    if name:
                        break
                if name:
                    break

        if not name:
            # 失败不写永久缓存：否则平台还没连上/一次超时就会永远没有昵称。
            self._session_name_fail_at[umo] = time.time()
            logger.debug(
                f"[{PLUGIN_NAME}] 查询会话昵称失败：umo={umo} "
                f"platform={platform_id} chat={chat_type} id={session_id}"
            )
            return ""

        self._session_name_cache[umo] = name
        self._session_name_fail_at.pop(umo, None)
        if len(self._session_name_cache) > MAX_SESSION_NAME_CACHE:
            oldest = next(iter(self._session_name_cache))
            self._session_name_cache.pop(oldest, None)
        # 昵称补上了，旧的列表缓存里还是空昵称，需要失效。
        self._invalidate_conversations_cache()
        logger.debug(f"[{PLUGIN_NAME}] 已获取会话昵称：umo={umo} name={name}")
        return name

    @staticmethod
    async def _call_platform_action(
        client: Any, action: str, **params: Any
    ) -> Any:
        """调用协议端 action，兼容 `client.api.call_action` 与 `client.call_action`。"""
        api = getattr(client, "api", None)
        call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            call_action = getattr(client, "call_action", None)
        if not callable(call_action):
            return None
        try:
            return await asyncio.wait_for(
                _maybe_await(call_action(action, **params)),
                timeout=SESSION_NAME_LOOKUP_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _session_name_from_result(result: Any, chat_type: str) -> str:
        if not isinstance(result, dict):
            return ""
        if chat_type == "group":
            keys = ("group_name", "groupName", "name")
        else:
            keys = ("nickname", "name", "card", "remark")
        for key in keys:
            value = str(result.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _history_stats(history: list[Any]) -> dict[str, Any]:
        messages = 0
        chars = 0
        snapshot_chars = 0
        has_snapshot = False
        for item in history:
            messages += 1
            text = _message_text(item)
            chars += len(text)
            if REFRESH_MARKER in text:
                has_snapshot = True
                snapshot_chars += len(text)
        return {
            "messages": messages,
            "chars": chars,
            "has_snapshot": has_snapshot,
            "snapshot_chars": snapshot_chars,
        }

    async def _load_all_conversations(self, manager: Any) -> list[Any]:
        getter = getattr(manager, "get_conversations", None)
        if not callable(getter):
            return []
        try:
            result = await _maybe_await(getter())
        except TypeError:
            return []
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 读取对话列表失败: {exc!r}")
            return []
        conversations = list(result or [])
        conversations.sort(
            key=lambda item: self._safe_int(getattr(item, "updated_at", 0)),
            reverse=True,
        )
        return conversations[:MAX_CONVERSATIONS]

    async def _current_cid(self, manager: Any, umo: str) -> str:
        getter = getattr(manager, "get_curr_conversation_id", None)
        if not callable(getter) or not umo:
            return ""
        try:
            return str(await _maybe_await(getter(umo)) or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _get_conversation(
        self, manager: Any, umo: str, cid: str
    ) -> Any:
        getter = getattr(manager, "get_conversation", None)
        if not callable(getter):
            return None
        try:
            return await _maybe_await(getter(umo, cid))
        except TypeError:
            return await _maybe_await(getter(umo, cid, False))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 读取对话失败: {exc!r}")
            return None

    @staticmethod
    def _load_history(conversation: Any) -> tuple[list[Any], str]:
        raw = getattr(conversation, "history", "") or "[]"
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            history = json.loads(raw) if isinstance(raw, str) else list(raw)
        except Exception:  # noqa: BLE001
            return [], "当前对话历史解析失败"
        if not isinstance(history, list):
            return [], "当前对话历史格式异常"
        return history, ""

    async def _restore_current_conversation(
        self, manager: Any, umo: str, original_cid: str, target_cid: str
    ) -> None:
        if not original_cid or original_cid == target_cid:
            return
        switcher = getattr(manager, "switch_conversation", None)
        if not callable(switcher):
            return
        try:
            await _maybe_await(switcher(umo, original_cid))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[{PLUGIN_NAME}] 恢复当前对话失败 umo={umo} "
                f"cid={original_cid}: {exc!r}"
            )

    async def api_conversations(self):
        """返回对话列表、机器人/会话聚合、快照状态与 WebUI 刷新状态。"""
        if json_response is None or error_response is None:
            return None
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return error_response("当前 AstrBot 版本不支持读取对话", status_code=500)
        await self._load_prompt_cache()

        query = getattr(request, "query", None)
        force = False
        if query is not None:
            try:
                force = str(query.get("refresh") or "").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                )
            except Exception:  # noqa: BLE001
                force = False
        cache_ttl = self._int("cache_ttl_seconds", 300, 0)
        if (
            not force
            and cache_ttl > 0
            and self._conversations_cache is not None
        ):
            cached_at, cached_payload = self._conversations_cache
            if time.time() - cached_at < cache_ttl:
                payload = dict(cached_payload)
                payload["cached"] = True
                payload["cache_age_seconds"] = int(time.time() - cached_at)
                return json_response(payload)

        try:
            conversations = await self._load_all_conversations(manager)
            platform_meta = self._platform_meta()
            current_by_umo: dict[str, str] = {}
            items: list[dict[str, Any]] = []
            for conversation in conversations:
                umo = str(getattr(conversation, "user_id", "") or "")
                cid = str(getattr(conversation, "cid", "") or "")
                if not cid:
                    continue
                if umo not in current_by_umo:
                    current_by_umo[umo] = await self._current_cid(manager, umo)
                history, _ = self._load_history(conversation)
                stats = self._history_stats(history)
                if stats["messages"] == 0:
                    continue
                umo_platform, message_type, session_id = self._split_umo(umo)
                platform_id = (
                    str(getattr(conversation, "platform_id", "") or "")
                    or umo_platform
                )
                chat_type = self._chat_type(message_type)
                bot_name = str(
                    platform_meta.get(platform_id, {}).get("name")
                    or platform_id
                    or ""
                )
                title = str(getattr(conversation, "title", "") or "").strip()
                cache = self._prompt_entry(umo, cid)
                items.append(
                    {
                        "key": self._cache_key(umo, cid),
                        "umo": umo,
                        "cid": cid,
                        "platform_id": platform_id,
                        "bot_name": bot_name,
                        "message_type": message_type,
                        "chat_type": chat_type,
                        "session_id": session_id,
                        "session_name": "",
                        "title": title,
                        "persona_id": str(
                            getattr(conversation, "persona_id", "") or ""
                        ),
                        "messages": stats["messages"],
                        "chars": stats["chars"],
                        "has_snapshot": stats["has_snapshot"],
                        "snapshot_chars": stats["snapshot_chars"],
                        "is_backup": "_backup" in title,
                        "is_current": bool(
                            current_by_umo.get(umo)
                            and current_by_umo[umo] == cid
                        ),
                        "can_refresh": bool(cache),
                        "prompt_chars": int(cache["chars"]) if cache else 0,
                        "prompt_cached_at": (
                            int(cache["updated_at"]) if cache else 0
                        ),
                        "created_at": self._safe_int(
                            getattr(conversation, "created_at", 0)
                        ),
                        "updated_at": self._safe_int(
                            getattr(conversation, "updated_at", 0)
                        ),
                    }
                )

            await self._attach_session_names(items)

            bots_map: dict[str, dict[str, Any]] = {}
            for item in items:
                platform_id = item["platform_id"] or "unknown"
                bot = bots_map.setdefault(
                    platform_id,
                    {
                        "platform_id": platform_id,
                        "name": item["bot_name"] or platform_id,
                        "type": "",
                        "conversations": 0,
                        "private": 0,
                        "group": 0,
                    },
                )
                bot["conversations"] += 1
                if item["chat_type"] == "group":
                    bot["group"] += 1
                elif item["chat_type"] == "private":
                    bot["private"] += 1
            for platform_id, meta in platform_meta.items():
                bot = bots_map.setdefault(
                    platform_id,
                    {
                        "platform_id": platform_id,
                        "name": meta.get("name") or platform_id,
                        "type": meta.get("type") or "",
                        "conversations": 0,
                        "private": 0,
                        "group": 0,
                    },
                )
                if meta.get("name"):
                    bot["name"] = meta["name"]
                if meta.get("type"):
                    bot["type"] = meta["type"]
            bots = sorted(
                bots_map.values(),
                key=lambda bot: (
                    -int(bot["conversations"]),
                    str(bot["name"]).lower(),
                ),
            )

            sessions_map: dict[str, dict[str, Any]] = {}
            for item in items:
                session = sessions_map.setdefault(
                    item["umo"],
                    {
                        "umo": item["umo"],
                        "platform_id": item["platform_id"],
                        "bot_name": item["bot_name"],
                        "message_type": item["message_type"],
                        "chat_type": item["chat_type"],
                        "session_id": item["session_id"],
                        "session_name": item["session_name"],
                        "conversations": 0,
                        "updated_at": 0,
                    },
                )
                session["conversations"] += 1
                if item["session_name"] and not session["session_name"]:
                    session["session_name"] = item["session_name"]
                session["updated_at"] = max(
                    session["updated_at"], item["updated_at"]
                )
            sessions = sorted(
                sessions_map.values(),
                key=lambda session: session["updated_at"],
                reverse=True,
            )

            totals = {
                "conversations": len(items),
                "sessions": len({item["umo"] for item in items}),
                "with_snapshot": sum(
                    1 for item in items if item["has_snapshot"]
                ),
                "refreshable": sum(1 for item in items if item["can_refresh"]),
            }
            payload = {
                "plugin": {
                    "name": PLUGIN_NAME,
                    "version": PLUGIN_VERSION,
                },
                "generated_at": int(time.time()),
                "cached": False,
                "cache_age_seconds": 0,
                "cache_ttl_seconds": cache_ttl,
                "refresh": {
                    "enabled": self._bool("auto_refresh_enabled", True),
                    "interval_minutes": min(
                        1440,
                        self._int("auto_refresh_interval", 60, 1),
                    ),
                },
                "ui": {"theme_color": self._theme_color()},
                "config": {
                    "backup_before_refresh": self._bool(
                        "backup_before_refresh", True
                    ),
                    "backup_conversation_title": self._str(
                        "backup_conversation_title", DEFAULT_BACKUP_TITLE
                    ),
                    "strip_trailing_period": self._bool(
                        "strip_trailing_period", True
                    ),
                },
                "totals": totals,
                "bots": bots,
                "sessions": sessions,
                "items": items,
            }
            if cache_ttl > 0:
                self._conversations_cache = (time.time(), payload)
            return json_response(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 读取 WebUI 对话列表失败: {exc!r}")
            return error_response(f"读取对话列表失败：{exc}", status_code=500)

    async def api_prompt(self):
        """返回已记录系统提示词中的人格片段，不返回完整提示词。"""
        if json_response is None or error_response is None:
            return None
        query = getattr(request, "query", None)
        umo = ""
        cid = ""
        if query is not None:
            try:
                umo = str(query.get("umo") or "").strip()
                cid = str(query.get("cid") or "").strip()
            except Exception:  # noqa: BLE001
                umo = cid = ""
        if not umo or not cid:
            return error_response("缺少会话或对话标识", status_code=400)
        await self._load_prompt_cache()
        cache = self._prompt_entry(umo, cid)
        if not cache or not cache.get("prompt"):
            return error_response(
                "该对话还没有记录到系统提示词", status_code=409
            )
        persona = str(cache.get("persona_prompt") or "")
        return json_response(
            {
                "umo": umo,
                "cid": cid,
                "prompt_chars": int(cache.get("chars") or 0),
                "cached_at": int(cache.get("updated_at") or 0),
                "persona_found": bool(persona),
                "persona_prompt": persona,
                "persona_chars": len(persona),
            }
        )

    async def api_refresh(self):
        """使用最近一次对话记录的系统提示词刷新指定对话。

        只操作对话数据，不向聊天平台发送消息。
        """
        if json_response is None or error_response is None:
            return None
        payload = await self._json_body()
        umo = str(payload.get("umo") or "").strip()
        cid = str(payload.get("cid") or "").strip()
        if not umo or not cid:
            logger.warning(
                f"[{PLUGIN_NAME}] WebUI 刷新未执行：缺少会话或对话标识 "
                f"umo={umo or '-'} cid={cid or '-'}"
            )
            return error_response("缺少会话或对话标识", status_code=400)

        def fail(message: str, status_code: int):
            """记录刷新未执行的原因，再把同样的原因返回给前端。"""
            logger.warning(
                f"[{PLUGIN_NAME}] WebUI 刷新未执行：{message} "
                f"umo={umo} cid={cid}"
            )
            return error_response(message, status_code=status_code)

        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return fail("当前 AstrBot 版本不支持对话管理", 500)
        await self._load_prompt_cache()
        cache = self._prompt_entry(umo, cid)
        if not cache or not cache.get("prompt"):
            return fail(
                "该对话还没有记录到系统提示词，请先在对话中发一条消息再刷新",
                409,
            )

        persona_override = payload.get("persona_prompt")
        selected_prompt = str(cache["prompt"])
        persona_changed = False
        if persona_override is not None:
            if not isinstance(persona_override, str):
                return fail("人格提示词格式不正确", 400)
            new_persona = persona_override.strip()
            # 以提示词里实际存在的人格段为准，避免缓存中的人格片段
            # （旧版本记录、人格配置原文等）与提示词不一致导致替换失败。
            old_persona = self._extract_persona_prompt(selected_prompt)
            replaced = None
            if old_persona:
                replaced = self._replace_persona_prompt(
                    selected_prompt, old_persona, new_persona
                )
            if replaced is None:
                cached_persona = str(cache.get("persona_prompt") or "")
                if cached_persona:
                    replaced = self._replace_persona_prompt(
                        selected_prompt, cached_persona, new_persona
                    )
            if replaced is None:
                # 提示词里找不到人格段：忽略人格修改，直接用完整提示词刷新。
                logger.warning(
                    f"[{PLUGIN_NAME}] 未找到人格提示词片段，忽略人格修改直接刷新 "
                    f"umo={umo} cid={cid}"
                )
                persona_override = None
            else:
                selected_prompt = replaced
                persona_changed = True

        conversation = await self._get_conversation(manager, umo, cid)
        if conversation is None:
            return fail("未找到该对话", 404)

        original_cid = await self._current_cid(manager, umo)
        event = _WebEvent(
            umo, str(getattr(conversation, "platform_id", "") or "")
        )
        req = _WebRequest(str(selected_prompt), conversation)
        try:
            ok, message = await self._perform_refresh(event, req)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] WebUI 刷新失败: {exc!r}")
            ok, message = False, f"刷新失败：{exc}"
        finally:
            await self._restore_current_conversation(
                manager, umo, original_cid, cid
            )

        if not ok:
            return fail(message, 500)
        if persona_changed:
            self._apply_prompt_override(
                umo, cid, selected_prompt, persona_override.strip()
            )
        return json_response({"ok": True, "message": self._reply_text(message)})

    async def api_copy(self):
        """把指定对话另存为 xxx_backup，原对话不变。

        只操作对话数据，不向聊天平台发送消息。
        """
        if json_response is None or error_response is None:
            return None
        payload = await self._json_body()
        umo = str(payload.get("umo") or "").strip()
        cid = str(payload.get("cid") or "").strip()
        if not umo or not cid:
            return error_response("缺少会话或对话标识", status_code=400)
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return error_response("当前 AstrBot 版本不支持对话管理", status_code=500)
        conversation = await self._get_conversation(manager, umo, cid)
        if conversation is None:
            return error_response("未找到该对话", status_code=404)
        history, parse_error = self._load_history(conversation)
        if parse_error:
            return error_response(parse_error, status_code=400)

        title_override = str(payload.get("title") or "").strip()[:255]
        original_cid = await self._current_cid(manager, umo)
        event = _WebEvent(
            umo, str(getattr(conversation, "platform_id", "") or "")
        )
        try:
            backup_cid, backup_error = await self._create_backup_conversation(
                manager=manager,
                event=event,
                umo=umo,
                conversation=conversation,
                history=history,
                old_cid=cid,
                title_override=title_override or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] WebUI 备份失败: {exc!r}")
            backup_cid, backup_error = None, f"备份失败：{exc}"
        finally:
            await self._restore_current_conversation(
                manager, umo, original_cid, cid
            )

        if not backup_cid:
            return error_response(backup_error, status_code=500)
        return json_response(
            {
                "ok": True,
                "backup_cid": backup_cid,
                "message": self._reply_text(
                    self._str("reply_copy_success", DEFAULT_COPY_SUCCESS_TEXT)
                ),
            }
        )

    async def api_delete_conversation(self):
        """删除指定对话及其全部聊天记录。

        只操作对话数据，不向聊天平台发送消息。
        """
        if json_response is None or error_response is None:
            return None
        payload = await self._json_body()
        umo = str(payload.get("umo") or "").strip()
        cid = str(payload.get("cid") or "").strip()
        if not umo or not cid:
            return error_response("缺少会话或对话标识", status_code=400)
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return error_response("当前 AstrBot 版本不支持对话管理", status_code=500)
        conversation = await self._get_conversation(manager, umo, cid)
        if conversation is None:
            return error_response("未找到该对话", status_code=404)
        deleter = getattr(manager, "delete_conversation", None)
        if not callable(deleter):
            return error_response("当前 AstrBot 版本不支持删除对话", status_code=500)
        try:
            await _maybe_await(deleter(umo, cid))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{PLUGIN_NAME}] 删除对话失败: {exc!r}")
            return error_response(f"删除对话失败：{exc}", status_code=500)

        self._invalidate_conversations_cache()
        logger.info(f"[{PLUGIN_NAME}] 已删除对话：umo={umo} cid={cid}")
        return json_response(
            {
                "ok": True,
                "message": self._reply_text(
                    self._str(
                        "reply_delete_success", DEFAULT_DELETE_SUCCESS_TEXT
                    )
                ),
            }
        )

    async def api_clear_snapshot(self):
        """移除指定对话中的系统提示词快照。

        只操作对话数据，不向聊天平台发送消息。
        """
        if json_response is None or error_response is None:
            return None
        payload = await self._json_body()
        umo = str(payload.get("umo") or "").strip()
        cid = str(payload.get("cid") or "").strip()
        if not umo or not cid:
            return error_response("缺少会话或对话标识", status_code=400)
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return error_response("当前 AstrBot 版本不支持对话管理", status_code=500)
        conversation = await self._get_conversation(manager, umo, cid)
        if conversation is None:
            return error_response("未找到该对话", status_code=404)
        history, parse_error = self._load_history(conversation)
        if parse_error:
            return error_response(parse_error, status_code=400)

        filtered = [
            item for item in history if not _is_refresh_snapshot(item)
        ]
        removed = len(history) - len(filtered)
        if removed:
            updater = getattr(manager, "update_conversation", None)
            if not callable(updater):
                return error_response(
                    "当前 AstrBot 版本不支持写入对话历史", status_code=500
                )
            try:
                await _maybe_await(updater(umo, cid, filtered))
            except TypeError:
                await _maybe_await(updater(umo, cid, history=filtered))
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 清除快照失败: {exc!r}")
                return error_response(f"清除快照失败：{exc}", status_code=500)
            self._invalidate_conversations_cache()

        message = (
            f"已清除 {removed} 条系统提示词快照"
            if removed
            else "没有可清除的系统提示词快照"
        )
        return json_response(
            {
                "ok": True,
                "removed": removed,
                "message": self._reply_text(message),
            }
        )
