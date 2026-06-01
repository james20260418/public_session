"""OpenClaw agent CLI 封装

驱动 OpenClaw agent 与用户进行增量对话。
一个 session 内，多次发消息会保持上下文累积。

核心接口：
    reply = generate_reply(user_message, session_id="sess-xxx")
    → 返回完整的最终回复文本（单字符串）

如果 OpenClaw 分多段回复，generate_reply 会自动等待所有段返回后合并。
"""

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from typing import Optional

# ── 常量 ────────────────────────────────────────────────────────────────

_AGENT_TIMEOUT = 180  # 单次 agent 调用超时（秒），大于 idle timeout 120s


# ── 返回结构 ──────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    """一次 agent 调用的完整结果"""
    full_text: str
    payloads: list[str]
    aborted: bool
    session_id: str
    model: str = ""


# ── 核心函数 ──────────────────────────────────────────────────────────


def run_temp_session(task: str, timeout: int = 60) -> Optional[str]:
    """开一个临时 OpenClaw session，执行一次性任务，返回输出文本。

    Args:
        task: 要执行的任务文本（如判断、搜索、摘要等）
        timeout: 超时秒数，默认 60s（轻量任务较短）

    Returns:
        str: agent 的完整回复文本，失败或超时返回 None。

    特性：
      - 每次调用使用随机 session ID，不累积上下文
      - session 用完即弃，不残留脏状态
      - session ID 使用 uuid 生成，不与其他调用冲突
      - 不会自动存入 memory（--json 模式不触发 memory hook）
    """
    session_id = f"temp-session-{uuid.uuid4().hex[:12]}"
    result = _call_agent(task, session_id, timeout)
    if result is None:
        return None
    return result.full_text if result.full_text else None


def generate_reply(
    user_message: str,
    session_id: str = "public-session-main",
    timeout: int = _AGENT_TIMEOUT,
) -> Optional[str]:
    """在指定 session 中发消息并等待完整回复。

    如果 session_id 已存在历史，消息会追加到已有上下文。
    区别于 --session-id=main，这里使用独立 session，不污染 webchat 历史。

    Args:
        user_message: 用户消息文本
        session_id: 会话标识，同一 session_id 保持增量上下文。
                    默认 "public-session-main"。
        timeout: agent 调用超时秒数

    Returns:
        str: 完整的最终回复文本（合并所有 payload 段），
             失败或超时返回 None。
    """
    result = _call_agent(user_message, session_id, timeout)
    if result is None:
        return None
    return result.full_text if result.full_text else None


# ── Session 清理 ──────────────────────────────────────────────────────────

def delete_session(session_key: str):
    """删除指定 session key 对应的 transcript 文件和 sessions.json 中的索引。

    Args:
        session_key: session key（如 agent:main:explicit:public-session-xxx），
                     或 session key 的后半部分（如 public-session-xxx）。

    用途:
        SCM 用完临时 session 后清理，不残留脏状态。
    """
    if not session_key:
        return

    agents_dir = os.path.expanduser("~/.openclaw/agents")
    if not os.path.isdir(agents_dir):
        return

    for agent_name in os.listdir(agents_dir):
        store_path = os.path.join(agents_dir, agent_name, "sessions", "sessions.json")
        if not os.path.exists(store_path):
            continue

        try:
            with open(store_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if not isinstance(data, dict):
            continue

        # 找到匹配的 session key
        matched_key = None
        matched_sid = None
        for k in data:
            if k == session_key or k.endswith(":" + session_key):
                matched_key = k
                matched_sid = data[k].get("sessionId", "")
                break

        if not matched_sid:
            continue

        # 删 transcript 文件
        transcript_path = os.path.join(
            os.path.dirname(store_path), f"{matched_sid}.jsonl")
        if os.path.exists(transcript_path):
            try:
                os.remove(transcript_path)
            except OSError:
                pass

        # 删索引
        del data[matched_key]
        try:
            with open(store_path, "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass
        return  # 删完就走，只处理一个 agent


def _call_agent(
    message: str,
    session_id: str,
    timeout: int,
) -> Optional[AgentResult]:
    """底层 agent 调用。"""
    cmd = [
        "openclaw", "agent",
        "--session-id", session_id,
        "--message", message,
        "--json",
    ]

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            proc.wait()
        return None
    except FileNotFoundError:
        return None

    if proc and proc.returncode != 0:
        return None

    stdout = (stdout or "").strip()
    if not stdout:
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None

    if data.get("status") != "ok":
        return None

    meta = data.get("result", {}).get("meta", {})
    agent_meta = meta.get("agentMeta", {})
    payloads_raw = data.get("result", {}).get("payloads", [])

    # 提取所有 payload 文本
    payload_texts = [p.get("text", "") for p in payloads_raw if p.get("text")]

    # 合并为完整回复
    full_text = "\n".join(payload_texts).strip()

    return AgentResult(
        full_text=full_text,
        payloads=payload_texts,
        aborted=meta.get("aborted", False),
        session_id=agent_meta.get("sessionId", ""),
        model=f"{agent_meta.get('provider', '')}/{agent_meta.get('model', '')}",
    )
