# 会话退出控制设计

## 背景

PM 型 agent 需要**高召回**的退出策略来防止 agent 间自反馈循环（无休止的礼貌性对话）。
当前 `SingleChatManager` 只有静态 idle timeout（300 秒）一种退出机制，对于 agent 间对话来说
太慢，且无法主动结束无意义循环。

## 两个退出机制

### 机制 A：发后等待（动态等待时长）

定义：agent 发出消息后，根据回复内容决定等多久再退出。

```
agent 回复 → 解析标识 → 决定等待时长 → 等待 / 立即结束
```

### 机制 B：收前拦截

定义：收到新消息后，在交给主 session 处理前，被拦截装置过滤掉，不触发对话。

## 本次实现

### 第一层：提示词标识引导 + Python 规则拦截

#### 特殊标识

Agent 在回复文本中嵌入以下标识，Python 侧解析并执行：

| 标识 | 含义 | Python 行为 |
|------|------|------------|
| `[!END]` | 结束会话 | 立即退出，不等 |
| `[!WAIT:N]` | 等 N 秒后退出 | clamp(N, 0, 60) 后等待再退出 |
| `[!SILENT]` | 不发送回复就结束 | 不调用 send_text，直接退出 |

默认行为（无任何标识）：极端高召回，视为 `[!END]`，立即退出。

#### 提示词引导

在 `_build_context_prefix` 末尾追加以下指导：

```
[退出控制]
如果你想结束本次对话，在回复末尾加上 [!END]。
如果你需要等待对方回复，在回复末尾加上 [!WAIT:N]，N 是等待秒数（0~60）。
如果你不需要发送消息、直接结束，回复 [!SILENT]。
默认情况下（不写任何标识），会话会在你回复后立即结束。
```

#### Python 解析逻辑

在 `_process_batch` 中，agent 回复后，扫描回复文本的**末尾**（最后 200 字符）中的标识：

```python
def _parse_exit_pragma(reply: str) -> tuple[str, int]:
    """解析回复中的退出标识。

    Returns:
        (clean_reply, wait_seconds)
        clean_reply — 去掉标识的纯净回复文本（供发送）
        wait_seconds — 等待秒数，0 表示立即退出，-1 表示不发送消息直接退出
    """
    tail = reply[-200:]
    # 优先级：SILENT > WAIT:N > END

    if "[!SILENT]" in tail:
        clean = reply.replace("[!SILENT]", "")
        return clean.strip(), -1

    if "[!WAIT:" in tail:
        # 提取 N
        import re
        m = re.search(r'\[!WAIT:(\d+)\]', tail)
        if m:
            n = int(m.group(1))
            n = max(0, min(n, 60))
            clean = reply.replace(f"[!WAIT:{n}]", "")
            return clean.strip(), n
        # 格式错误，按 END 处理
        clean = reply.replace(m.group(0) if m else '', '')
        return clean.strip(), 0

    if "[!END]" in tail:
        clean = reply.replace("[!END]", "")
        return clean.strip(), 0  # 立即退出

    # 无标识 → 高召回默认：立即退出
    return reply.strip(), 0
```

### 第二层（本次不做）：会话目的评估器

异步外挂 session 在对话结束后判断：
- 本次会话目的是否达成？
- 是否跑题需要重开？

需要更高层的 PM 能力设计，留待后续 PR。

## 退出流程图

```
_process_batch 开始
  │
  ├─ 收消息
  ├─ 加 Typing
  ├─ 调 OpenClaw → reply
  ├─ 解析标识 → (clean_reply, wait_seconds)
  │
  ├─ wait_seconds == -1 → 不打 SLEEP 直接结束（SILENT）
  │
  ├─ wait_seconds >= 0 → 发 clean_reply
  │
  ├─ 打 Done
  ├─ 更新 last_processed
  │
  │ wait_seconds > 0?
  │   ├─ Yes → 轮询新消息，等 wait_seconds 秒
  │   │        到期无新消息 → 结束
  │   └─ No  → 立即结束
  │
  └─ 退出
```

## 架构影响

- **不改动** `public_session.py`（OneTick 循环）
- **不改动** `scheduler.py`
- **微小改动** `single_chat_manager.py`：`_process_batch` 增加标识解析 + 等待逻辑
- **新增** `util/exit_pragma.py`：标识解析纯函数
- **改动** `_build_context_prefix`：追加提示词指导
