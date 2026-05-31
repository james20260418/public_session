"""message_manager.py - Feishu WS listener + message snapshot.

Functionality:
  - WS long connection: receives messages, stores in internal table
  - snapshot(): thread-safe deep copy of all messages (for OneTick polling)
  - NameResolver: lazy-cached open_id → name mapping via contact API
  - on_message callback (optional, for event-driven consumers)
  - REST: send_text(open_id, text), react(message_id, emoji)
"""

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from util.feishu import (get_token, send_text_message, _build_post_content, _request,
                          react_message, delete_reaction, get_reactions,
                          download_resource)

logger = logging.getLogger("message_manager")

# ── 常量 ────────────────────────────────────────────────────────────────

_LOG_ID_TRIM = 18      # 日志中 message_id 截断长度
_LOG_SENDER_ID_TRIM = 12   # 日志中 sender_id 截断长度
_LOG_SENDER_ID_FULL = 24   # 日志中 sender_id 完整显示长度
_LOG_TEXT_TRIM = 30        # 日志中消息文本截断长度

# ── 文件相关常量 ───────────────────────────────────────────────

_FILE_DOWNLOAD_TIMEOUT = 60   # 下载文件超时（秒）


# ── NameResolver ────────────────────────────────────────────────────────

class NameResolver:
    """Lazy-cached open_id → name mapping.

    通过飞书 contact API 查询用户名字，结果永久缓存（名字不会频繁变化）。
    线程安全，支持并发查询。
    """

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def resolve(self, open_id: str) -> Optional[str]:
        """获取 open_id 对应的用户名字。

        优先返回缓存结果。缓存未命中时调用 contact API 查询。
        解析失败（API 不可用、名字不存在）时返回 None。

        Args:
            open_id: 飞书用户的 open_id

        Returns:
            用户名字（中文名），或 None（解析失败）
        """
        with self._lock:
            cached = self._cache.get(open_id)
            if cached is not None:
                return cached if cached else None

        token = get_token(self._app_id, self._app_secret)
        if not token:
            return None

        result = _request(f"/contact/v3/users/{open_id}", token)
        user = result.get("data", {}).get("user", {})
        name = user.get("name", "") or ""

        with self._lock:
            # 缓存结果：空字符串表示已查询过但无名字
            self._cache[open_id] = name

        return name if name else None

    def cached_name(self, open_id: str) -> Optional[str]:
        """仅返回缓存中的名字，不触发 API 调用。

        Args:
            open_id: 飞书用户的 open_id

        Returns:
            缓存的名字，或 None（未缓存）
        """
        with self._lock:
            return self._cache.get(open_id)


# ── Message ───────────────────────────────────────────────────────────

class Message:
    """Feishu message with metadata."""

    __slots__ = ("message_id", "sender_id", "sender_name", "text",
                 "create_time", "recv_time")

    def __init__(self, message_id: str, sender_id: str, sender_name: str,
                 text: str, create_time: str = "0", recv_time: float = 0.0):
        self.message_id = message_id
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.text = text
        self.create_time = create_time
        self.recv_time = recv_time

    def __repr__(self):
        return (f"Message(id={self.message_id[:_LOG_ID_TRIM]}, "
                f"sender={self.sender_name}({self.sender_id[:_LOG_SENDER_ID_TRIM]}), "
                f"text={self.text[:_LOG_TEXT_TRIM]})")


# ── MessageTable ──────────────────────────────────────────────────────

class MessageTable:
    """Thread-safe message store.

    Messages grouped by sender_id (open_id). Each sender gets a list
    of Message objects, newest first.

    Consumer reads via deep-copy snapshot. No dedup — WS won't deliver
    the same message twice.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._by_sender: dict[str, list[Message]] = {}

    def add(self, message_id: str, sender_id: str, text: str, create_time: str,
            sender_name: str) -> Message:
        msg = Message(
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            create_time=create_time,
            recv_time=time.time(),  # 本机接收时间，用于 debounce
        )
        with self._lock:
            if sender_id not in self._by_sender:
                self._by_sender[sender_id] = []
            self._by_sender[sender_id].insert(0, msg)
        return msg

    def snapshot(self) -> dict[str, list[Message]]:
        """Deep copy of {sender_id: [Message, ...]}, newest first."""
        with self._lock:
            return copy.deepcopy(self._by_sender)


# ── MessageManager ────────────────────────────────────────────────────

class MessageManager:
    """Feishu message manager.

    - WS background thread receives and stores messages in MessageTable.
    - snapshot() for OneTick polling (thread-safe deep copy).
    - NameResolver built-in: automatically resolves sender names via contact API.
    - on_message callback for event-driven consumers (optional).
    - send_text / react helpers.

    Usage:
        mgr = MessageManager(app_id, app_secret)
        mgr.start()
        ...
        table = mgr.snapshot()  # OneTick polling
        send_text("ou_xxx", "hello")
        mgr.stop()
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Optional[Callable[[Message], None]] = None,
        mark_get_on_receive: bool = False,
        log_file: str = "",
        log_to_stdout: bool = True,
        file_storage_dir: str = "",
    ):
        self._app_id = app_id
        self._app_secret = app_secret
        self._table = MessageTable()
        self._name_resolver = NameResolver(app_id, app_secret)
        self._on_message = on_message
        self._mark_get_on_receive = mark_get_on_receive
        self._log_file = log_file
        self._log_to_stdout = log_to_stdout
        self._file_storage_dir = file_storage_dir
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── lifecycle ──

    def start(self):
        if self._ws_thread and self._ws_thread.is_alive():
            logger.warning("MessageManager already running")
            return
        self._stop_event.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_loop, name="msg-mgr-ws", daemon=True,
        )
        self._ws_thread.start()
        logger.info("MessageManager started")

    def stop(self):
        self._stop_event.set()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=10)
        logger.info("MessageManager stopped")

    # ── name resolver (public) ──

    def resolve_name(self, open_id: str) -> Optional[str]:
        """获取 open_id 对应的用户名字。

        委托给内置的 NameResolver，首次调用自动缓存。

        Returns:
            用户名字（中文名），或 None（解析失败）
        """
        return self._name_resolver.resolve(open_id)

    # ── snapshot (thread-safe) ──

    def snapshot(self) -> dict[str, list[tuple[str, str, str, str]]]:
        """Thread-safe deep copy of all messages.

        Returns:
            {sender_id: [Message, ...]}
            Each list is newest-first.
        """
        return self._table.snapshot()

    # ── REST helpers ──

    def send_text(self, open_id: str, text: str) -> dict:
        token = get_token(self._app_id, self._app_secret)
        if not token:
            return {"code": -1, "msg": "token failed"}
        return send_text_message(open_id, token, text)

    def send_text_to_chat(self, chat_id: str, text: str) -> dict:
        content = _build_post_content(text)
        token = get_token(self._app_id, self._app_secret)
        if not token:
            return {"code": -1, "msg": "token failed"}
        return _request(
            "/im/v1/messages?receive_id_type=chat_id",
            token=token,
            method="POST",
            body={"receive_id": chat_id, "msg_type": "post", "content": content},
        )

    def react(self, message_id: str, emoji: str = "Done") -> dict:
        token = get_token(self._app_id, self._app_secret)
        if not token:
            return {"code": -1, "msg": "token failed"}
        return react_message(message_id, token, emoji=emoji)

    def mark_typing(self, message_id: str) -> dict:
        """给消息加 typing indicator（Typing 表情）"""
        return self.react(message_id, emoji="Typing")

    def mark_done(self, message_id: str) -> dict:
        """去掉 typing indicator，换成 Done 表情

        先给消息加 Done，然后尝试删除现有的 Typing reaction。
        删除失败不影响结果（可能没有 Typing reaction 或已被删）。
        """
        # 先加 Done
        result = self.react(message_id, emoji="Done")
        if result.get("code") != 0:
            return result
        # 查询 Typing reaction 的 ID，然后删除它
        token = get_token(self._app_id, self._app_secret)
        if not token:
            return result  # Done 已成功，Typing 删不掉也无所谓
        reactions = get_reactions(message_id, token)
        if reactions.get("code") != 0:
            return result
        for item in reactions.get("data", {}).get("items", []):
            rt = item.get("reaction_type", {})
            if rt.get("emoji_type") == "Typing":
                delete_reaction(message_id, item.get("reaction_id", ""), token)
                break
        return {"code": 0, "msg": "Done marked, Typing cleaned up"}

    # ── WS loop ──

    @staticmethod
    def _extract_text(msg_obj) -> str:
        """从文本消息对象中提取纯文本内容。"""
        raw = getattr(msg_obj, 'content', '')
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed.get("text", str(parsed))
            return str(parsed)
        except (json.JSONDecodeError, TypeError):
            return str(raw)

    def _ts(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _log_line(self, line: str):
        """写日志到 stdout 和/或文件。"""
        if self._log_to_stdout:
            print(line, flush=True)
        if self._log_file:
            od = os.path.dirname(self._log_file)
            if od:
                os.makedirs(od, exist_ok=True)
            with open(self._log_file, "a") as f:
                f.write(line + "\n")

    def _get_file_storage_dir(self) -> str:
        """获取文件存储目录，不存在时自动创建。"""
        d = self._file_storage_dir
        if not d:
            log_dir = os.path.dirname(self._log_file) if self._log_file else "."
            parent = os.path.dirname(os.path.normpath(log_dir))
            d = os.path.join(parent, "received_files")
        os.makedirs(d, exist_ok=True)
        return d

    def _build_file_output_path(self, storage_dir: str, sender_id: str,
                                file_name: str, message_id: str) -> str:
        """构造文件下载路径。

        received_files/
          └── ou_xxx/
              └── 2026-05-21/
                  └── om_xxx_xxx.pdf
        """
        date_str = datetime.now().strftime("%Y-%m-%d")
        safe_name = file_name if file_name else "unknown_file"
        return os.path.join(storage_dir, sender_id, date_str,
                            f"{message_id}_{safe_name}")

    def _download_and_store_file(self, msg_obj, message_id: str, msg_type: str,
                                  sender_id: str, sender_name: str):
        """下载非文本消息的资源文件到本地，不回写 MessageTable。

        文件只存不表（table 只放文本消息），由 agent 在对话中按需翻找。
        同一个 message_id 不会重复下载（已存在的路径直接跳过）。

        成功：日志记录 "📎 sender: received xxx.pdf -> /path"
        失败：日志记录 "⚠️ sender: download failed: reason"
        """
        raw = getattr(msg_obj, 'content', '')
        if not raw:
            self._log_line(f"[{self._ts()}] ⚠️  {sender_name}: 文件消息无 content (type={msg_type}), id={message_id[:_LOG_ID_TRIM]}")
            return

        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self._log_line(f"[{self._ts()}] ⚠️  {sender_name}: 文件消息 content 解析失败 (type={msg_type}), id={message_id[:_LOG_ID_TRIM]}")
            return

        if not isinstance(parsed, dict):
            self._log_line(f"[{self._ts()}] ⚠️  {sender_name}: 文件消息 content 非 dict (type={msg_type}), id={message_id[:_LOG_ID_TRIM]}")
            return

        file_key = parsed.get("file_key", "") or ""
        image_key = parsed.get("image_key", "") or ""
        file_name = parsed.get("file_name", "") or "unknown"

        if not file_key and not image_key:
            self._log_line(f"[{self._ts()}] ⚠️  {sender_name}: 文件消息无 file_key/image_key (type={msg_type}), id={message_id[:_LOG_ID_TRIM]}")
            return

        key = file_key if file_key else image_key
        resource_type = "file" if file_key else "image"

        storage_dir = self._get_file_storage_dir()
        output_path = self._build_file_output_path(storage_dir, sender_id,
                                                    file_name, message_id)

        # 去重：已存在的文件不重复下载
        if os.path.exists(output_path):
            size_kb = os.path.getsize(output_path) / 1024
            self._log_line(f"[{self._ts()}] 📎  {sender_name}: 文件已存在 (跳过) {file_name} ({size_kb:.1f}KB) -> {output_path}")
            return

        token = get_token(self._app_id, self._app_secret)
        if not token:
            self._log_line(f"[{self._ts()}] ⚠️  {sender_name}: 下载文件失败 (token), id={message_id[:_LOG_ID_TRIM]}")
            return

        result = download_resource(
            message_id, key, token,
            resource_type=resource_type,
            output_path=output_path,
            timeout=_FILE_DOWNLOAD_TIMEOUT,
        )

        if result.get("code") == 0:
            size_kb = result.get("size", 0) / 1024
            path = result.get("path", output_path)
            self._log_line(f"[{self._ts()}] 📎  {sender_name}: 收到文件 {file_name} ({size_kb:.1f}KB) -> {path}")
        else:
            error_msg = result.get("msg", "未知错误")
            self._log_line(f"[{self._ts()}] ⚠️  {sender_name}: 文件 {file_name} 下载失败: {error_msg}")

    def _ws_loop(self):
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        except ImportError:
            logger.error("lark-oapi not installed, run: pip install lark-oapi")
            return

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_ws_message)
            .build()
        )

        ws_client = lark.ws.Client(
            self._app_id, self._app_secret,
            event_handler=handler, log_level=lark.LogLevel.WARNING,
        )

        while not self._stop_event.is_set():
            try:
                ws_client.start()
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.warning(f"WS lost ({e}), reconnecting in 5s...")
                time.sleep(5)

    def _on_ws_message(self, data: "P2ImMessageReceiveV1") -> None:
        event = data.event
        if not event or not event.message:
            return

        msg_obj = event.message
        message_id = getattr(msg_obj, 'message_id', '')
        if not message_id:
            return

        msg_type = getattr(msg_obj, 'message_type', '') or ''
        create_time = getattr(msg_obj, 'create_time', '0')

        # sender 信息在 event.sender 上，不在 message.sender 里
        event_sender = getattr(event, 'sender', None)
        sender_id = ''
        if event_sender:
            sender_id_obj = getattr(event_sender, 'sender_id', None)
            if sender_id_obj is not None:
                sender_id = getattr(sender_id_obj, 'open_id', '') or ''

        if not sender_id:
            logger.error(
                f"Dropping msg {message_id[:_LOG_ID_TRIM]}: empty sender_id. "
                f"event.sender={event_sender!r}"
            )
            return

        sender_name = self._name_resolver.resolve(sender_id)
        if not sender_name:
            logger.error(
                f"Dropping msg {message_id[:_LOG_ID_TRIM]}: name resolve failed. "
                f"sender_id={sender_id[:_LOG_SENDER_ID_FULL]}"
            )
            return

        if msg_type == "text":
            # ── 文本消息：写入 MessageTable（供 OneTick 消费）──
            text = self._extract_text(msg_obj)
            msg = self._table.add(message_id, sender_id, text, create_time,
                                  sender_name)

            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            preview = msg.text[:10].replace("\n", " ")
            self._log_line(f"[{ts}] 📩 {msg.sender_name}: {preview}... [{len(msg.text)}chars]")

            # optional callback & auto-reaction
            if self._mark_get_on_receive:
                try:
                    self.react(message_id, emoji="Get")
                except Exception as e:
                    logger.error(f"auto Get reaction failed: {e}")

            if self._on_message:
                try:
                    self._on_message(msg)
                except Exception as e:
                    logger.error(f"on_message callback failed: {e}")
        else:
            # ── 非文本消息：只存文件，不入 MessageTable ──
            self._download_and_store_file(msg_obj, message_id, msg_type,
                                          sender_id, sender_name)
