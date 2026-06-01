"""
exit_pragma.py — 会话退出标识解析

解析 agent 回复中的 [!END]、[!WAIT]、[!SILENT] 标识，
提取纯净回复文本和退出策略。

标识设计原则：
  - 用 [!xxx] 而非 [END] 避免与普通对话内容冲突
  - 只扫描回复末尾 200 字符（减少误匹配）
  - 标识一旦匹配就被移除，不发送给用户

退出策略：
  - [!END] / 默认 → exit_immediately=True，处理完立即退出
  - [!WAIT]       → exit_immediately=False，进入等待模式（走 idle timeout）
  - [!SILENT]     → silent=True，不发消息直接退出

参见 docs/session-exit-control.md
"""

import re

# ── 常量 ────────────────────────────────────────────────────────────────

# 只在回复末尾这个范围内扫描标识
_SCAN_TAIL_CHARS = 200


# ── 返回结构 ──────────────────────────────────────────────────────────

class ExitPragma:
    """解析结果

    Attributes:
        clean_reply: 去掉所有标识后的纯净回复文本
        exit_immediately: True=立即退出, False=进入等待模式（走 idle timeout）
        silent: True=不发消息直接退出
    """
    __slots__ = ("clean_reply", "exit_immediately", "silent")

    def __init__(self, clean_reply: str, exit_immediately: bool = True,
                 silent: bool = False):
        self.clean_reply = clean_reply
        self.exit_immediately = exit_immediately
        self.silent = silent


# ── 解析函数 ──────────────────────────────────────────────────────────

def parse_exit_pragma(reply: str) -> ExitPragma:
    """解析回复中的退出标识。

    扫描回复的最后 _SCAN_TAIL_CHARS 字符，按优先级匹配：
      1. [!SILENT]  → silent=True, 不发消息直接退出
      2. [!WAIT]    → exit_immediately=False, 进入等回复模式
      3. [!END]     → exit_immediately=True, 立即退出
      4. 无标识      → exit_immediately=True（高召回默认）

    Args:
        reply: agent 的原始回复文本

    Returns:
        ExitPragma
    """
    if not reply:
        return ExitPragma("", exit_immediately=True)

    tail = reply[-_SCAN_TAIL_CHARS:]

    # 优先级 1：SILENT — 不发送消息直接退出
    if "[!SILENT]" in tail:
        clean = reply.replace("[!SILENT]", "")
        return ExitPragma(clean.strip(), exit_immediately=True, silent=True)

    # 优先级 2：WAIT — 进入等回复模式
    if "[!WAIT]" in tail:
        clean = reply.replace("[!WAIT]", "")
        return ExitPragma(clean.strip(), exit_immediately=False)

    # 优先级 3：END — 立即退出
    if "[!END]" in tail:
        clean = reply.replace("[!END]", "")
        return ExitPragma(clean.strip(), exit_immediately=True)

    # 默认：高召回，立即退出
    return ExitPragma(reply.strip(), exit_immediately=True)
