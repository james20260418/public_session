"""
single_chat_manager v2 — 重写会话管理器

与 v1 (single_chat_manager.py) 的区别：

1. **Init 阶段 — 读记忆**：
   - Python 直接读取 per-person public context (PPPC) 文件，毫秒级
   - 在 prompt 中提示 agent 通过 memory_search 补充原生日记

2. **Finalize 阶段 — 写记忆**：
   - 主 session 生成 PPPC 摘要（通过 generate_reply，不走飞书）
   - Python 解析后写入文件
   - 主 session 发 /new 触发 OpenClaw 原生日记 hook

3. **关键提示词生成**：
   - 所有模板化的 prompt 封装成纯函数，输入变量、输出文本
   - 方便阅读和修改

用法：
    from single_chat_manager.v2 import SingleChatManagerV2
    mgr = SingleChatManagerV2(config, message_manager, ws_root, token_provider)
    result = mgr.run(candidate)
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import CachedTokenProvider
from message_manager import MessageManager
from scheduler import Candidate
from single_chat_manager.single_chat_manager import _load_last_processed
from util.openclaw import generate_reply

# ── 常量 ────────────────────────────────────────────────────────────────

_POLL_INTERVAL = 0.5        # 轮询间隔 0.5 秒
_DEBOUNCE_SECONDS = 1       # 发现新消息后等 1 秒再处理
_IDLE_TIMEOUT = 120         # 无新消息超时（秒）
_LOG_ID_TRIM = 18           # 日志中 message_id 截断长度
_SESSION_ID_PREFIX = "public-session-"  # OpenClaw session ID 前缀，per sender
_PPPC_RECENT_DAYS = 7       # 读取最近几天的 PPPC 文件
_AGENT_TIMEOUT = 180        # generate_reply 超时（秒）
_PPPC_TIMEOUT = 60          # PPPC 摘要生成超时（秒）
_DIARY_TIMEOUT = 15         # /new 原生日记超时（秒）

HKT = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════════════
# 提示词模板（纯函数）
# ═══════════════════════════════════════════════════════════════════════


def build_context_instruction(pppc_text: str) -> str:
    """构建 init 阶段的前情提要指令。

    放在用户新消息之前，告知 agent 该 sender 的历史对话背景。

    Args:
        pppc_text: 从 PPPC 文件读取的对话历史摘要（可为空）

    Returns:
        要拼接到用户消息前的 context 文本，内容为空时返回空字符串。
    """
    if not pppc_text.strip():
        return ""

    return (
        f"[公共对话历史]\n"
        f"以下是该用户之前的对话记录（公共会话记忆），请注意这是背景信息，\n"
        f"请结合以上背景回复用户当前的消息。\n"
        f"{pppc_text.strip()}\n"
        f"[/公共对话历史]"
    )


def build_pppc_summary_prompt(name: str, open_id: str,
                              message_count: int,
                              timed_out: bool) -> str:
    """构建 finalize 阶段生成 PPPC 摘要的 prompt。

    主 session 按此 prompt 生成摘要，回复以 [PPPC_START]...[/PPPC_END] 格式。

    Args:
        name: 发送者显示名
        open_id: 发送者 open_id
        message_count: 本次对话处理的消息数
        timed_out: 是否超时结束

    Returns:
        Agent prompt 文本
    """
    return (
        f"## 指令\n"
        f"请为本次 public session 对话生成一个简洁的摘要（PPPC — "
        f"Per-Person Public Context），用于下次对话时加载为前情提要。\n"
        f"\n"
        f"## 对话信息\n"
        f"- 参与者: {name} ({open_id})\n"
        f"- 处理消息数: {message_count}\n"
        f"- 是否超时: {'是' if timed_out else '否'}\n"
        f"\n"
        f"## 输出格式\n"
        f"请只输出以下格式的摘要，不要包含其他任何内容：\n"
        f"\n"
        f"[PPPC_START]\n"
        f"- 日期: {{对话日期}}\n"
        f"- 主题: {{一句话概括本次对话主题}}\n"
        f"- 摘要: {{2-5句话描述对话内容、关键决策、重要信息}}\n"
        f"[/PPPC_END]\n"
        f"\n"
        f"如果本次对话没有实质内容（如只有打招呼），只输出 [PPPC_START][/PPPC_END]。"
    )


# ═══════════════════════════════════════════════════════════════════════
# PPPC 文件读写（纯函数）
# ═══════════════════════════════════════════════════════════════════════


def get_pppc_dir(workspace_root: str, open_id: str) -> str:
    """返回某个 sender 的 PPPC 文件目录。

    Args:
        workspace_root: 工作区根目录（如 /james_pm/public_session）
        open_id: 发送者 open_id

    Returns:
        目录路径
    """
    return os.path.join(workspace_root, "memory", "public-session", open_id)


def read_recent_pppc(workspace_root: str, open_id: str,
                     days: int = _PPPC_RECENT_DAYS) -> str:
    """读取该 sender 最近 N 天的 PPPC 文件。

    Args:
        workspace_root: 工作区根目录
        open_id: 发送者 open_id
        days: 读取最近多少天的文件

    Returns:
        拼接后的文本（最近的文件在前），没有文件时返回空字符串。
    """
    pppc_dir = get_pppc_dir(workspace_root, open_id)
    if not os.path.isdir(pppc_dir):
        return ""

    now = datetime.now(HKT)
    texts: list[str] = []
    for i in range(days):
        date_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        path = os.path.join(pppc_dir, f"{date_str}.md")
        try:
            with open(path) as f:
                content = f.read().strip()
            if content:
                texts.append(f"# {date_str}\n{content}")
        except (OSError, FileNotFoundError):
            continue

    return "\n\n".join(texts)


def write_pppc(workspace_root: str, open_id: str,
               content: str) -> Optional[str]:
    """将 PPPC 摘要写入文件。

    解析 [PPPC_START]...[/PPPC_END] 标签，提取纯内容后写入。

    Args:
        workspace_root: 工作区根目录
        open_id: 发送者 open_id
        content: Agent 回复文本（含 [PPPC_START]...[/PPPC_END] 标签）

    Returns:
        写入的文件路径，失败（解析不到标签）时返回 None。
    """
    start_tag = "[PPPC_START]"
    end_tag = "[/PPPC_END]"
    start_idx = content.find(start_tag)
    end_idx = content.find(end_tag)

    if start_idx < 0 or end_idx < 0:
        return None

    pure = content[start_idx + len(start_tag):end_idx].strip()
    if not pure:
        return None

    date_str = datetime.now(HKT).strftime("%Y-%m-%d")
    pppc_dir = get_pppc_dir(workspace_root, open_id)
    os.makedirs(pppc_dir, exist_ok=True)
    path = os.path.join(pppc_dir, f"{date_str}.md")

    try:
        with open(path, "w") as f:
            f.write(pure + "\n")
        return path
    except OSError:
        return None


# ═══════════════════════════════════════════════════════════════════════
# 对话结果
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class ChatResult:
    """单次 single chat 的执行结果。"""
    message_count: int = 0
    timed_out: bool = False


# ═══════════════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════════════


def _log_line(text: str, c: Candidate, log_file: Optional[str] = None):
    """输出一行日志到 stdout 和可选的文件。"""
    ts = datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {text}"
    print(line, flush=True)
    if log_file:
        try:
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# 主类
# ═══════════════════════════════════════════════════════════════════════

class SingleChatManagerV2:
    """Single Chat Manager v2 — 完整的单次会话管理。

    职责：
      - 多消息 debounce（等用户连续输入完）
      - idle 超时检测
      - init 加载 PPPC 上下文
      - finalize 生成 PPPC 摘要 + 触发原生日记
      - 所有飞书交互通过 message_manager 代理
    """

    def __init__(self,
                 config,
                 message_manager: MessageManager,
                 ws_root: str,
                 token_provider: CachedTokenProvider,
                 log_file: Optional[str] = None,
                 stop_file: Optional[str] = None):
        """
        Args:
            config: 应用配置
            message_manager: 消息管理器
            ws_root: 工作区根目录（如 /james_pm/public_session）
            token_provider: token 提供者
            log_file: 日志文件路径（可选）
            stop_file: stop 文件路径（可选）
        """
        self._config = config
        self._mgr = message_manager
        self._workspace_root = ws_root
        self._token_provider = token_provider
        self._log_file = log_file
        self._stop_file = stop_file

        # 运行状态
        self._result = ChatResult()
        self._context_prefix: str = ""   # init 阶段加载的 PPPC 上下文
        self._session_id: str = ""       # 本次使用的 OpenClaw session ID

    # ── run ──────────────────────────────────────────────────────────

    def run(self, c: Candidate) -> ChatResult:
        """执行一次完整的 single chat。

        Args:
            c: 选中的 Candidate

        Returns:
            ChatResult: 执行结果
        """
        self._result = ChatResult()
        self._session_id = f"{_SESSION_ID_PREFIX}{c.sender_id}"

        # ── Init：加载 PPPC 上下文 ──
        pppc = read_recent_pppc(self._workspace_root, c.sender_id)
        self._context_prefix = build_context_instruction(pppc)
        if self._context_prefix:
            _log_line(f"📖 PPPC 已加载 ({len(pppc)} chars)",
                      c, self._log_file)
        else:
            _log_line(f"📖 无 PPPC 历史", c, self._log_file)

        # ── 开始会话 ──
        name = c.sender_name or c.sender_id

        # 从持久化文件读上次处理断点，避免 new run 重复处理旧消息
        last_msg_id = _load_last_processed(self._config).get(c.sender_id)
        _log_line(f"[chat {name}] 📞 开始会话 (v2, pp={len(pppc)}chars, "
                  f"last={last_msg_id[:_LOG_ID_TRIM] if last_msg_id else 'none'})",
                  c, self._log_file)

        # v2 不发送打招呼消息

        last_activity = time.time()

        while True:
            if self._should_stop():
                break

            snapshot = self._mgr.snapshot()
            my_msgs = snapshot.get(c.sender_id) or []
            new_msgs = self._filter_new_messages(my_msgs, last_msg_id)

            if new_msgs:
                # debounce：等用户连续输入
                self._wait_debounce(c, last_msg_id)

                # debounce 期间可能被 stop，不要再继续处理
                if self._should_stop():
                    break

                # 重新取最新快照
                snapshot = self._mgr.snapshot()
                my_msgs = snapshot.get(c.sender_id) or []
                new_msgs = self._filter_new_messages(my_msgs, last_msg_id)

                if new_msgs:
                    _log_line(f"📦 开始处理 batch ({len(new_msgs)} 条, "
                              f"last_msg_id={last_msg_id[:_LOG_ID_TRIM] if last_msg_id else 'none'})",
                              c, self._log_file)
                    last_msg_id = new_msgs[-1].message_id
                    self._process_batch(c, new_msgs)
                    # 每批处理后即时持久化，异常退出也不会丢进度
                    _sync_last_processed(self._config, c.sender_id, last_msg_id)
                    self._result.message_count += len(new_msgs)
                    last_activity = time.time()
                    continue

            # idle 超时检测
            idle = time.time() - last_activity
            if idle > _IDLE_TIMEOUT:
                self._result.timed_out = True
                _log_line(f"[chat {name}] ⏰ idle 超时 ({_IDLE_TIMEOUT}s)",
                          c, self._log_file)
                break

            time.sleep(_POLL_INTERVAL)

        # ── Finalize: PPPC 摘要 + 原生日记 ──
        self._finalize(c)

        _log_line(f"✅ 会话结束，共处理 {self._result.message_count} 条消息 (v2)",
                  c, self._log_file)
        return self._result

    # ── 消息处理 ─────────────────────────────────────────────────────

    def _filter_new_messages(self, msgs: list,
                             last_msg_id: Optional[str]) -> list:
        """从消息列表中过滤出比 last_msg_id 更新的消息。

        snapshot 中 msgs 按 create_time 逆序（最新在前）。

        Args:
            msgs: 按 create_time 逆序的消息列表（最新的在前面）
            last_msg_id: 上次处理的最后一条消息 ID

        Returns:
            新消息列表（按时间正序，最早的在前面）。
        """
        if not msgs:
            return []

        if last_msg_id is None:
            return list(reversed(msgs))

        # 找到 last_msg_id 的位置（snapshot 最新在前，所以 idx
        # 及之前是已处理过的，之后的是新的）
        try:
            idx = next(i for i, m in enumerate(msgs)
                       if m.message_id == last_msg_id)
        except StopIteration:
            return list(reversed(msgs))

        # idx 之前的包含了新的（因为 snapshot 里最新的在 0 号位）
        return list(reversed(msgs[:idx]))

    def _wait_debounce(self, c: Candidate,
                       last_msg_id: Optional[str]):
        """等 debounce 时间，期间有新消息则重置等待。"""
        deadline = time.time() + _DEBOUNCE_SECONDS
        while time.time() < deadline:
            if self._should_stop():
                return
            time.sleep(_POLL_INTERVAL)

            cur = self._mgr.snapshot()
            cur_msgs = cur.get(c.sender_id) or []
            if last_msg_id is not None:
                try:
                    idx = next(i for i, m in enumerate(cur_msgs)
                               if m.message_id == last_msg_id)
                    if idx > 0:  # 有新消息出现在队列前面
                        deadline = time.time() + _DEBOUNCE_SECONDS
                except StopIteration:
                    pass

    def _process_batch(self, c: Candidate, batch: list):
        """处理一批用户消息（debounce 后）。

        流程：
          1. 给所有消息打 Typing 表情
          2. 拼接 prompt（PPPC 上下文 + memory_search 提示 + 用户消息）
          3. 调 OpenClaw agent 生成回复
          4. 打 Done 表情 + 删 Typing
          5. 通过飞书 API 发送回复
        """
        name = c.sender_name or c.sender_id

        # ── 1. Typing 表情 ──
        for msg in batch:
            tr = self._mgr.mark_typing(msg.message_id)
            if tr.get("code") != 0:
                _log_line(f"⚠️  mark_typing failed: {tr}", c, self._log_file)

        # ── 2. 拼 prompt → 调 agent ──
        user_text = self._merge_batch_to_prompt_text(batch)
        if self._context_prefix:
            full_prompt = (
                f"{self._context_prefix}\n\n"
                f"你可以使用 memory_search 搜索近期的原生日记作为补充背景信息。\n\n"
                f"## 用户新消息\n"
                f"{user_text}\n\n"
                f"请回复用户的新消息。"
            )
        else:
            full_prompt = (
                f"{user_text}\n\n"
                f"请回复用户的新消息。"
            )

        _log_line(f"🤖 调 OpenClaw agent "
                  f"(session={self._session_id[:40]}..., {len(full_prompt)}chars)...",
                  c, self._log_file)

        reply = generate_reply(
            full_prompt,
            session_id=self._session_id,
            timeout=_AGENT_TIMEOUT,
        )

        # ── 3. Done 表情 + 删 Typing ──
        for msg in batch:
            dr = self._mgr.mark_done(msg.message_id)
            if dr.get("code") != 0:
                _log_line(f"⚠️  mark_done failed: {dr}", c, self._log_file)

        # ── 4. 发送回复或标记异常 ──
        if reply:
            _log_line(f"💬 agent 回复 ({len(reply)} chars)", c, self._log_file)
            self._mgr.send_text(c.sender_id, reply)
        else:
            _log_line(f"⚠️  agent 无回复（超时或失败），标记 No 表情", c, self._log_file)
            last_msg = batch[-1]
            err_react = self._mgr.react(last_msg.message_id, emoji="No")
            if err_react.get("code") != 0:
                _log_line(f"⚠️  No 表情标记失败: {err_react}", c, self._log_file)

    @staticmethod
    def _merge_batch_to_prompt_text(batch: list) -> str:
        """将 batch 中的多条消息合并为一段文本。"""
        if len(batch) == 1:
            return batch[-1].text
        lines = []
        for msg in batch:
            n = msg.sender_name or "匿名"
            lines.append(f"{n}: {msg.text}")
        return "\n".join(lines)

    # ── Finalize ────────────────────────────────────────────────────

    def _finalize(self, c: Candidate):
        """会话结束收尾。

        流程：
          1. message_count == 0 → 直接返回
          2. 主 session 生成 PPPC 摘要（不发飞书）
          3. Python 写入文件
          4. 主 session 发 /new 触发原生日记 hook
        """
        if self._result.message_count <= 0:
            return

        name = c.sender_name or c.sender_id
        prompt = build_pppc_summary_prompt(
            name, c.sender_id,
            self._result.message_count,
            self._result.timed_out,
        )

        _log_line(f"📝 Finalize: 生成 PPPC 摘要...", c, self._log_file)
        pppc_raw = generate_reply(
            prompt,
            session_id=self._session_id,
            timeout=_PPPC_TIMEOUT,
        )

        if pppc_raw:
            path = write_pppc(self._workspace_root, c.sender_id, pppc_raw)
            if path:
                _log_line(f"📝 PPPC 已写入 {path}", c, self._log_file)
            else:
                _log_line(f"⚠️  PPPC 解析失败（未找到标签）", c, self._log_file)
                _log_line(f"    agent 回复: {pppc_raw[:200]}", c, self._log_file)
        else:
            _log_line(f"⚠️  PPPC 生成失败（agent 无回复）", c, self._log_file)

        # 发 /new 触发原生日记保存
        _log_line(f"📝 触发原生日记保存 (/new)...", c, self._log_file)
        generate_reply(
            "/new",
            session_id=self._session_id,
            timeout=_DIARY_TIMEOUT,
        )
        _log_line(f"✅ 原生日记保存已触发", c, self._log_file)

    # ── Stop ─────────────────────────────────────────────────────────

    def _should_stop(self) -> bool:
        """检查外部终止条件（stop 文件是否存在）。"""
        if self._stop_file and os.path.exists(self._stop_file):
            return True
        return False


# ═══════════════════════════════════════════════════════════════════════
# last_processed 持久化
# ═══════════════════════════════════════════════════════════════════════


def _ensure_last_processed_path(config) -> str:
    """获取 last_processed.json 路径，创建目录。"""
    d = config.state_dir or os.path.dirname(config.log_file or ".")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "last_processed.json")


def _sync_last_processed(config, sender_id: str, msg_id: str):
    """将 sender 的处理进度持久化到 last_processed.json。

    供 run() 结束时调用，确保调度器下次不会重复选中同一个 sender。
    """
    path = _ensure_last_processed_path(config)
    try:
        with open(path) as f:
            lp = json.load(f)
        if not isinstance(lp, dict):
            lp = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        lp = {}

    lp[sender_id] = msg_id
    try:
        with open(path, "w") as f:
            json.dump(lp, f, indent=2)
    except OSError:
        pass
