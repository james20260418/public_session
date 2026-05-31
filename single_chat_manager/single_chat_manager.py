"""
single_chat_manager — 单次完整会话的执行器

SingleChatManager 负责一次完整的 single chat：从选定 sender、前情提要、
执行对话到退出，全部在 run() 中阻塞完成。

一次 run() = 一个完整的"接待-聊天-结束"周期。

版本历史：
  - 传声筒：PM bot 回复固定的"看到了"
  - 接入 OpenClaw：将用户消息发给 openclaw agent 生成真实回复
  - 记忆上下文 + Finalize：添加 chat init 前情提要（temp session search）
    和会话结束记忆归档（主 session 总结 + temp session 写文件）
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import CachedTokenProvider
from message_manager import Message, MessageManager
from scheduler import Candidate
from util.openclaw import generate_reply

# ── 常量 ────────────────────────────────────────────────────────────────

_IDLE_TIMEOUT = 120         # 120 秒无回复超时（agent 可能调工具等较久）
_POLL_INTERVAL = 0.5        # 轮询间隔 0.5 秒
_DEBOUNCE_SECONDS = 1       # 发现新消息后等 1 秒再处理
_LOG_ID_TRIM = 18           # 日志中 message_id 截断长度
_SESSION_ID_PREFIX = "public-session-"  # OpenClaw session ID 前缀，per sender
_PPPC_MAX_CHARS = 1000      # PPPC 文件保留最近多少字符
_DIARY_GEN_TIMEOUT = 30    # 主 session 生成日记摘要超时（秒）

HKT = timezone(timedelta(hours=8))

# ── 数据结构 ────────────────────────────────────────────────────────────

@dataclass
class ChatResult:
    """一次 single chat 的执行结果"""
    sender_id: str
    sender_name: str
    message_count: int = 0      # 本次会话处理的消息数量
    timed_out: bool = False
    error: Optional[str] = None
    processed_msgs: list = field(default_factory=list)  # [(sender_name, text, reply_text), ...]


@dataclass
class _SessionState:
    """运行时状态"""
    last_msg_id: str            # 已经处理到的消息 id
    last_activity: float        # 上次有消息被处理的时间戳


# ── 日志 ────────────────────────────────────────────────────────────────

def _ts():
    return datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S")


def _log_line(msg: str, candidate: Candidate, log_file: str = ""):
    line = f"[{_ts()}] [chat {candidate.sender_name}] {msg}"
    print(line, flush=True)
    if log_file:
        od = os.path.dirname(log_file)
        if od:
            os.makedirs(od, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(line + "\n")


# ── Last Processed 持久化 ──────────────────────────────────────────────

def _ensure_last_processed_path(config) -> str:
    d = config.state_dir or os.path.dirname(config.log_file or ".")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "last_processed.json")


def _load_last_processed(config) -> dict[str, str]:
    path = _ensure_last_processed_path(config)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {k: str(v) for k, v in raw.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_last_processed(config, lp: dict[str, str]):
    path = _ensure_last_processed_path(config)
    with open(path, "w") as f:
        json.dump(lp, f, indent=2)


def _read_pppc_files(workspace_root: str, open_id: str,
                       max_chars: int = _PPPC_MAX_CHARS) -> str:
    """读取某个 sender 的所有 PPPC 文件，按时间逆序拼接不超过 max_chars。

    Args:
        workspace_root: 工作区根目录
        open_id: 发送者 open_id
        max_chars: 最大字符数限制

    Returns:
        拼接后的最近对话文本，不超过 max_chars 字符。
    """
    pppc_dir = os.path.join(
        workspace_root, "memory", "public-session", open_id)
    if not os.path.isdir(pppc_dir):
        return ""

    try:
        files = sorted(os.listdir(pppc_dir), reverse=True)
    except OSError:
        return ""

    segments: list[str] = []
    total = 0
    for fname in files:
        if not fname.endswith(".md"):
            continue
        path = os.path.join(pppc_dir, fname)
        try:
            with open(path) as f:
                content = f.read().strip()
        except OSError:
            continue
        if not content:
            continue

        if total + len(content) > max_chars:
            # 截取这个文件的一部分
            room = max_chars - total
            if room > 0:
                segments.append(content[:room])
            break

        segments.append(content)
        total += len(content)

    if not segments:
        return ""

    # segments 是最新的在前，拼接时最新的放最后（更自然）
    segments.reverse()
    separator = f"\n{'─'*40}\n"
    text = separator.join(segments)

    # 如果超过限制（因为 separator 可能超出一点点）
    if len(text) > max_chars:
        text = text[-max_chars:]

    return text


# ── SingleChatManager ──────────────────────────────────────────────────

class SingleChatManager:
    """一次完整 single chat 的执行器。

    MVP（传声筒版本）：
      - 从 snapshot 中读取指定 sender 的新消息
      - 2 秒 debounce：发现消息后等待 2 秒，等用户连续发完再一并处理
      - 对一批新消息：回复一条"看到了" → 标记 Done → 更新 last_processed
      - 30 秒无新消息 → 超时结束

    run() 是唯一公共入口，阻塞执行，返回 ChatResult。
    """

    def __init__(
        self,
        config,
        mgr: MessageManager,
        token_provider: CachedTokenProvider,
        candidate: Candidate,
    ):
        self._config = config
        self._mgr = mgr
        self._token_provider = token_provider
        self._candidate = candidate
        self._log_file = config.log_file or ""
        assert config.state_dir, "state_dir 必须在 config 中配置"
        self._workspace_root = os.path.dirname(os.path.normpath(config.state_dir))
        self._file_storage_dir = getattr(config, 'file_storage_dir', '') or ""

        self._result = ChatResult(
            sender_id=candidate.sender_id,
            sender_name=candidate.sender_name,
        )
        self._session_id = ""  # init 后由 run() 设置

    # ── 公共入口 ──

    def run(self) -> ChatResult:
        """执行一次完整的 single chat。

        阻塞执行，包含从前情提要、聊天到退出的完整周期。

        Returns:
            ChatResult: 本次会话的结果
        """
        c = self._candidate
        self._session_id = f"{_SESSION_ID_PREFIX}{c.sender_id}"

        # 初始化状态：从 last_processed 拿到此 sender 的处理断点
        lp = _load_last_processed(self._config)
        last_msg_id = lp.get(c.sender_id, "")

        state = _SessionState(
            last_msg_id=last_msg_id,
            last_activity=time.time(),
        )

        _log_line(f"📞 开始会话 (last={last_msg_id[:_LOG_ID_TRIM] or 'none'})",
                  c, self._log_file)

        # 等待循环：poll → debounce → batch process → 超时退出
        while True:
            now = time.time()

            # 外置 stop 文件检测（如 stop.sh 生成的终止标记）
            if self._should_stop():
                _log_line("🛑  Stop file detected, exiting single chat",
                          c, self._log_file)
                break

            new_msgs = self._poll_new_messages(c, state, now)
            if new_msgs:
                state.last_activity = now
                self._process_batch(c, new_msgs, state)
                continue

            # 超时检查
            idle = now - state.last_activity
            if idle >= _IDLE_TIMEOUT:
                _log_line(f"⏱️  超时（{_IDLE_TIMEOUT}s 无消息）",
                          c, self._log_file)
                self._result.timed_out = True
                break

            time.sleep(_POLL_INTERVAL)

        # ── 会话结束：主 session 生成 PPPC 摘要 + /new 原生日记 ──
        self._finalize(c)

        _log_line(f"✅ 会话结束，共处理 {self._result.message_count} 条消息",
                  c, self._log_file)
        return self._result

    # ── 前情提要 ──

    def _build_context_prefix(self, c: Candidate) -> str:
        """构建 init 阶段的前情提要。

        包含四部分：
          1. 对话对象身份说明
          2. 提示使用 memory_search 搜索原生日记
          3. 对方发送的文件已自动保存到本地（如有文件目录）
          4. PPPC 中的原始对话记录
        """
        assert c.sender_name, f"sender {c.sender_id} 必须有名"
        name = c.sender_name

        parts = [
            f"你正在与 {name} 对话，请用 {name} 称呼对方。",
            "",
            "你可以使用 memory_search 搜索近期的原生日记作为补充背景信息。",
        ]

        # 如果配置了文件存储目录，告知 agent 对方发送的文件已自动存到本地
        if self._file_storage_dir:
            file_path = os.path.join(self._file_storage_dir, c.sender_id)
            parts.extend([
                "",
                f"[文件存储] {name} 发送的文件已自动保存到本地，"
                f"路径: {file_path}/",
                f"如有需要，可以读取这些文件来获取信息。",
            ])

        # 发送文件工具说明
        send_file_script = os.path.join(
            self._workspace_root, "util", "send_file.py")
        if os.path.exists(send_file_script):
            parts.extend([
                "",
                f"[可用工具] 你可以通过以下命令发送文件给 {name}：",
                f"  python -m util.send_file <open_id> <file_path>",
                f"  其中 open_id 是对方的 open_id: {c.sender_id}",
                f"  支持常见文档、图片、音视频、压缩包等格式。",
                f"  示例：python -m util.send_file {c.sender_id} /path/to/file.pdf",
            ])

        raw = _read_pppc_files(self._workspace_root, c.sender_id)
        if raw:
            parts.extend([
                "",
                f"[之前的对话记录]",
                raw.strip(),
                f"[/之前的对话记录]",
            ])

        return "\n".join(parts)

    # ── PPPC 辅助（纯函数）──

    @staticmethod
    def _build_pppc_dialogue(msgs: list) -> str:
        """将本轮对话消息格式化为原始对话文本。

        Args:
            msgs: (speaker_name, user_text, reply_text) 元组列表

        Returns:
            格式化后的对话文本，截取最近约 1000 chars。
        """
        lines = []
        for speaker, text, reply in msgs:
            lines.append(f"{speaker}: {text}")
            lines.append(f"AI: {reply}")
        text = "\n".join(lines)

        # 截取最近约 1000 chars
        if len(text) <= _PPPC_MAX_CHARS:
            return text
        return "...(省略之前的内容)\n" + text[-_PPPC_MAX_CHARS:]

    def _get_pppc_dir(self, open_id: str) -> str:
        """返回某个 sender 的 PPPC 文件目录。"""
        return os.path.join(
            self._workspace_root, "memory", "public-session", open_id)

    def _build_diary_entry(self, c: Candidate) -> str:
        """调主 session 生成原生日记摘要。

        由主 session 自己决定存储路径和方式，不在此处写死路径。

        Returns:
            日记 markdown 文本（可能为空），调用方负责处理写入。
        """
        assert c.sender_name, f"sender {c.sender_id} 必须有名"
        prompt = (
            f"你正在和 public session 的用户 {c.sender_name} 对话。"
            f"请收到此消息后，根据本次对话的内容，"
            f"将关键信息记入你的原生日记（memory/YYYY-MM-DD.md）。\n"
            f"记住：\n"
            f"1. 使用 /new 命令将记忆归档\n"
            f"2. 只记录有意义的内容，不要做无意义的记录\n"
            f"3. 日记应该是抽象总结，不是原始对话\n"
            f"4. 本次会话共处理 {self._result.message_count} 条消息"
            f"{'（因超时结束）' if self._result.timed_out else ''}\n"
        )
        _log_line(f"📝 Diary: 让主 session 自己存记忆...", c, self._log_file)
        result = generate_reply(
            prompt,
            session_id=self._session_id,
            timeout=_DIARY_GEN_TIMEOUT,
        )
        if result and result.strip():
            return result.strip()
        return ""

    def _write_pppc_raw(self, open_id: str, content: str) -> Optional[str]:
        """将原始对话文本写入 PPPC 文件（按对话时间命名）。"""
        ts = datetime.now(HKT).strftime("%Y-%m-%d_%H%M%S")
        pppc_dir = self._get_pppc_dir(open_id)
        os.makedirs(pppc_dir, exist_ok=True)
        path = os.path.join(pppc_dir, f"{ts}.md")

        try:
            with open(path, "w") as f:
                f.write(content + "\n")
            return path
        except OSError:
            return None

    # ── 会话结束收尾 ──

    def _finalize(self, c: Candidate):
        """会话结束收尾：将原始对话写入 PPPC 文件 + /new 触发原生日记。

        流程：
          1. message_count == 0 → 直接返回
          2. 将 processed_msgs 格式化为原始对话文本，截取最近 ~1000 chars
          3. 写入 PPPC 文件
          4. 发 /new 触发 OpenClaw 原生日记 hook
        """
        if self._result.message_count <= 0:
            return

        # ── 格式化为原始对话文本 ──
        dialogue = self._build_pppc_dialogue(self._result.processed_msgs)
        if not dialogue.strip():
            return

        _log_line(f"📝 Finalize: 写 PPPC 原始对话 ({len(dialogue)} chars)...",
                  c, self._log_file)
        path = self._write_pppc_raw(c.sender_id, dialogue)
        if path:
            _log_line(f"📝 PPPC 已写入 {path}", c, self._log_file)
        else:
            _log_line(f"⚠️  PPPC 写入失败", c, self._log_file)

        # 让主 session 自己存原生日记（不在此处写死路径）
        self._build_diary_entry(c)

    # ── Stop 检测 ──

    def _should_stop(self) -> bool:
        """检查外部终止条件。

        当 public_session 同款的 stop 文件存在时，返回 True。
        不删除文件，由外层 cleanup 处理。
        """
        stop_file = os.path.expanduser(self._config.stop_file)
        return bool(stop_file and os.path.exists(stop_file))

    def _poll_new_messages(self, c: Candidate, state: _SessionState, now: float
                           ) -> list[tuple[str, str, str, str, float]]:
        """获取指定 sender 的所有新消息。

        规则：
          1. 从 snapshot 中找到所有比 last_msg_id 更新的消息
          2. debounce：最新消息的 recv_time 距现在 < 2 秒，
             认为用户还在输入中，返回空列表
          3. 返回所有新消息，按创建时间升序排列

        Returns:
            list of Message，按创建时间升序。空列表表示无新消息或仍在 debounce。
        """
        table = self._mgr.snapshot()
        msgs = table.get(c.sender_id, [])
        if not msgs:
            return []

        # 找出所有比 last_msg_id 更新的消息
        if state.last_msg_id:
            try:
                idx = next(i for i, msg in enumerate(msgs)
                           if msg.message_id == state.last_msg_id)
            except StopIteration:
                idx = -1  # 断点不在 snapshot 中，全部是新消息
        else:
            idx = -1

        if idx == 0:
            return []  # 无新消息

        # 提取所有新消息
        #   idx > 0: msgs[0..idx-1] 是新消息
        #   idx < 0: 全部 msgs 都是新消息
        new_segment = msgs[:idx] if idx > 0 else msgs[:]
        # new_segment 是 newest-first，反转成 oldest-first
        raw = list(reversed(new_segment))

        # debounce：最新消息的 recv_time 距现在 < 2 秒，认为还在输入中
        if now - raw[-1].recv_time < _DEBOUNCE_SECONDS:
            return []

        return raw

    def _process_batch(self, c: Candidate,
                       batch: list[tuple[str, str, str, str, float]],
                       state: _SessionState):
        """处理一批新消息。

        Args:
            batch: 按创建时间升序排列的消息列表
        """
        # 日志：收到的所有消息
        for msg in batch:
            preview = msg.text[:10].replace("\n", " ")
            _log_line(
                f"💬 {msg.sender_name}: {preview}... [{len(msg.text)}chars]",
                c, self._log_file,
            )

        # ── 1. 给 batch 中所有消息加 typing indicator（Typing 表情）──
        for msg in batch:
            tr = self._mgr.mark_typing(msg.message_id)
            if tr.get("code") != 0:
                _log_line(
                    f"⚠️  typing 标记失败 ({msg.message_id[:_LOG_ID_TRIM]}): "
                    f"{tr.get('msg', '')}",
                    c, self._log_file,
                )

        # ── 2. 前情提要（有记忆的话第一次 batch 才会触发）──
        ctx = getattr(self, '_context_prefix', '')
        if not ctx:
            prefix = self._build_context_prefix(c)
            self._context_prefix = prefix or ""
            if prefix:
                _log_line(f"📖 前情提要已加载 ({len(prefix)} chars)",
                          c, self._log_file)

        # ── 3. 将 batch 合并后交给 OpenClaw 生成回复 ──
        session_id = f"{_SESSION_ID_PREFIX}{c.sender_id}"

        # 拼接用户消息
        if len(batch) == 1:
            user_text = batch[-1].text
        else:
            lines = []
            for msg in batch:
                lines.append(f"{msg.sender_name}: {msg.text}")
            user_text = "\n".join(lines)

        # 如果有前情提要，拼接为 system-style 上下文前缀
        ctx = getattr(self, '_context_prefix', '')
        if ctx:
            prompt = (
                f"[对话背景]\n"
                f"{ctx}\n\n"
                f"[用户新消息]\n"
                f"{user_text}\n\n"
                f"请回复用户的新消息。注意：对话背景是历史纪要，不要重复已说过的内容。"
            )
        else:
            prompt = user_text

        _log_line(f"🤖 调 OpenClaw agent 生成回复 "
                  f"(session={session_id[:40]}..., {len(prompt)}chars)...",
                  c, self._log_file)

        reply_text = generate_reply(prompt, session_id=session_id)
        if not reply_text:
            # ── 异常：OpenClaw 未返回回复 ──
            # 对最后一条消息打异常表情标记（不发送消息，避免触发对方 agent）
            _log_line("⚠️  OpenClaw 未返回回复，标记异常表情", c, self._log_file)
            last_msg = batch[-1]
            err_react = self._mgr.react(last_msg.message_id, emoji="No")
            if err_react.get("code") != 0:
                _log_line(
                    f"⚠️  异常表情标记失败 ({last_msg.message_id[:_LOG_ID_TRIM]}): "
                    f"{err_react.get('msg', '')}",
                    c, self._log_file,
                )
            self._result.error = "agent call failed (timeout or error)"

            # 清理 typing 标记
            for msg in batch:
                self._mgr.mark_done(msg.message_id)

            # 更新 last_processed
            lp = _load_last_processed(self._config)
            lp[c.sender_id] = last_msg.message_id
            _save_last_processed(self._config, lp)
            state.last_msg_id = last_msg.message_id
            return

        _log_line(f"🤖 OpenClaw 回复: {reply_text[:40]}... [{len(reply_text)}chars]",
                  c, self._log_file)

        # ── 记录到 processed_msgs ──
        for msg in batch:
            self._result.processed_msgs.append(
                (msg.sender_name, msg.text, reply_text))

        # ── 3. 通过飞书 bot 发送回复 ──
        token = self._token_provider.get()
        if not token:
            _log_line("⚠️  无 token，跳过回复", c, self._log_file)
            return

        reply_result = self._mgr.send_text(c.sender_id, reply_text)
        if reply_result.get("code") != 0:
            _log_line(
                f"⚠️  发送回复给 {c.sender_name} 失败: "
                f"{reply_result.get('msg', '')}",
                c, self._log_file,
            )
        else:
            preview = reply_text[:10].replace("\n", " ")
            _log_line(
                f"✅ 已发送回复给 {c.sender_name}: {preview}... [{len(reply_text)}chars]",
                c, self._log_file,
            )

        # ── 4. batch 处理完成，把所有 typing indicator 换成 Done ──
        for msg in batch:
            dr = self._mgr.mark_done(msg.message_id)
            if dr.get("code") != 0:
                _log_line(
                    f"⚠️  Done 标记失败 ({msg.message_id[:_LOG_ID_TRIM]}): "
                    f"{dr.get('msg', '')}",
                    c, self._log_file,
                )

        # 更新 last_processed 到最新消息
        last_msg = batch[-1]
        lp = _load_last_processed(self._config)
        lp[c.sender_id] = last_msg.message_id
        _save_last_processed(self._config, lp)

        state.last_msg_id = last_msg.message_id

        self._result.message_count += len(batch)
