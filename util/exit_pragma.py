"""
exit_pragma.py — 会话退出标识解析

解析 agent 回复中的 [!END]、[!WAIT:N]、[!SILENT] 标识，
提取纯净回复文本和等待策略。

标识设计原则：
  - 用 [!xxx] 而非 [END] 避免与普通对话内容冲突
  - 只扫描回复末尾 200 字符（减少误匹配）
  - 标识一旦匹配就被移除，不发送给用户

参见 docs/session-exit-control.md
"""

import re

# ── 常量 ────────────────────────────────────────────────────────────────

# 只在回复末尾这个范围内扫描标识
_SCAN_TAIL_CHARS = 200

# WAIT 参数的合法范围
_WAIT_MIN = 0
_WAIT_MAX = 60

# 特殊 wait_seconds 值
#   -1 = SILENT（不发消息直接退出）
#    0 = 立即退出（END / 默认）


# ── 返回结构 ──────────────────────────────────────────────────────────

class ExitPragma:
    """解析结果

    Attributes:
        clean_reply: 去掉所有标识后的纯净回复文本
        wait_seconds: 等待时间（秒），-1=SILENT, 0=END, >0=WAIT:N
    """
    __slots__ = ("clean_reply", "wait_seconds")

    def __init__(self, clean_reply: str, wait_seconds: int):
        self.clean_reply = clean_reply
        self.wait_seconds = wait_seconds


# ── 解析函数 ──────────────────────────────────────────────────────────

def parse_exit_pragma(reply: str) -> ExitPragma:
    """解析回复中的退出标识。

    扫描回复的最后 _SCAN_TAIL_CHARS 字符，按优先级匹配：
      1. [!SILENT]  → wait=-1, 不发送消息直接退出
      2. [!WAIT:N]  → wait=N (clamped 0~60)
      3. [!END]     → wait=0, 立即退出
      4. 无标识      → wait=0（高召回默认）

    Args:
        reply: agent 的原始回复文本

    Returns:
        ExitPragma(clean_reply, wait_seconds)
    """
    if not reply:
        return ExitPragma("", 0)

    tail = reply[-_SCAN_TAIL_CHARS:]

    # 优先级 1：SILENT — 不发送消息直接退出
    if "[!SILENT]" in tail:
        clean = reply.replace("[!SILENT]", "")
        return ExitPragma(clean.strip(), -1)

    # 优先级 2：WAIT:N — 等 N 秒
    m = re.search(r'\[!WAIT:(\d+)\]', tail)
    if m:
        n = int(m.group(1))
        n = max(_WAIT_MIN, min(n, _WAIT_MAX))
        clean = reply.replace(f"[!WAIT:{m.group(1)}]", "")
        return ExitPragma(clean.strip(), n)

    # 优先级 3：END — 立即退出
    if "[!END]" in tail:
        clean = reply.replace("[!END]", "")
        return ExitPragma(clean.strip(), 0)

    # 默认：高召回，立即退出
    return ExitPragma(reply.strip(), 0)
