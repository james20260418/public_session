"""
exit_pragma.py — 会话退出标识解析

解析 agent 回复中的 NO_REPLY、[!WAIT] 标识，
提取纯净回复文本并判断是否静默退出。

标识设计原则：
  - 用 [!xxx] 避免与普通对话内容冲突
  - NO_REPLY 是 OpenClaw 框架级沉默回复标记，优先级最高
  - 只扫描回复末尾 50 字符（减少误匹配）
  - 标识一旦匹配就被移除，不发送给用户

退出策略：
  - NO_REPLY → silent=True，不发消息直接退出（OpenClaw 框架同款机制）
  - [!WAIT]   → 正常等待（走 idle timeout），清除标识
  - 无标识     → 走 idle timeout（与 [!WAIT] 同义）

参见 docs/session-exit-control.md
"""

# ── 常量 ────────────────────────────────────────────────────────────────

# 只在回复末尾这个范围内扫描标识
_SCAN_TAIL_CHARS = 50


# ── 返回结构 ──────────────────────────────────────────────────────────

class ExitPragma:
    """解析结果

    Attributes:
        clean_reply: 去掉所有标识后的纯净回复文本
        silent: True=不发消息直接退出，clean_reply 强制为 ""
    """
    __slots__ = ("clean_reply", "silent")

    def __init__(self, clean_reply: str, silent: bool = False):
        self.clean_reply = clean_reply
        self.silent = silent


# ── 解析函数 ──────────────────────────────────────────────────────────

def parse_exit_pragma(reply: str) -> ExitPragma:
    """解析回复中的退出标识。

    扫描回复的最后 _SCAN_TAIL_CHARS 字符，按优先级匹配：
      1. NO_REPLY   → silent=True, 不发消息直接退出（OpenClaw 框架级沉默回复）
      2. [!WAIT]    → 正常等待（走 idle timeout），清除标识
      3. 无标识      → 正常等待（同 WAIT）

    NO_REPLY 是 OpenClaw 框架的沉默回复机制：当 agent 认为没有需要回复的内容时，
    输出 "NO_REPLY" 字符串。此机制属于 OpenClaw 框架行为，非 DeepSeek 专有特性。

    Args:
        reply: agent 的原始回复文本

    Returns:
        ExitPragma
    """
    if not reply:
        return ExitPragma("")

    tail = reply[-_SCAN_TAIL_CHARS:]

    # 优先级 1：NO_REPLY — 框架级沉默回复（后缀扫描）
    # 可以是 "NO_REPLY"、"NO_REPLY."、或前面有礼貌用语再加 NO_REPLY
    if "NO_REPLY" in tail:
        return ExitPragma("", silent=True)

    # 优先级 2：WAIT — 进入等回复模式（清除标识）
    if "[!WAIT]" in tail:
        clean = reply.replace("[!WAIT]", "")
        return ExitPragma(clean.strip())

    # 默认：正常等待（走 idle timeout）
    return ExitPragma(reply.strip())
