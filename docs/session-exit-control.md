# 会话退出控制设计

## 背景

PM 型 agent 需要**高召回**的退出策略来防止 agent 间自反馈循环（无休止的礼貌性对话）。
当前 `SingleChatManager` 只有静态 idle timeout（300 秒）一种退出机制，对于 agent 间对话来说
太慢，且无法主动结束无意义循环。

## 退出标识

Agent 在回复文本中嵌入以下标识，Python 侧解析并执行：

| 标识 | 含义 | Python 行为 |
|------|------|------------|
| `[!WAIT]` | 回复后等对方回复（默认行为） | `exit_immediately=False`，不做处理，走 idle timeout（300 秒）|
| `[!SILENT]` | 不发送回复就结束 | 不调 `send_text`，直接退出 |
| 无标识 | 正常等待 | 走 idle timeout |

## Python 解析逻辑 (`util/exit_pragma.py`)

```python
def parse_exit_pragma(reply: str) -> ExitPragma:
    # 扫描回复最后 200 字符
    # 匹配优先级：SILENT > WAIT > END > 默认(立即退出)
    # 返回 (clean_reply, exit_immediately, silent)
```

## 退出流程图

```
_process_batch 开始
  │
  ├─ 收消息 → 加 Typing → 调 OpenClaw → reply
  ├─ 解析标识 → (clean_reply, exit_immediately, silent)
  │
  ├─ silent=True → 不发消息，直接退出
  │
  ├─ 发 clean_reply → 打 Done → 更新 last_processed
  │
  ├─ exit_immediately=True → last_activity = 0
  │    → run() 循环下一个 idle check 命中 → 退出
  │
  └─ exit_immediately=False = [!WAIT]
       → last_activity 保持当前时间
       → run() 循环自然走 idle timeout (300s)
```

## 架构影响

- **不改动** `public_session.py`（OneTick 循环）
- **不改动** `scheduler.py`
- **微小改动** `single_chat_manager.py`：`_process_batch` 增加标识解析 + exit 控制
- **新增** `util/exit_pragma.py`：标识解析纯函数
- **改动** `_build_context_prefix`：追加提示词指导
