# 会话退出控制设计

## 背景

PM 型 agent 需要**高召回**的退出策略来防止 agent 间自反馈循环（无休止的礼貌性对话）。
当前 `SingleChatManager` 只有静态 idle timeout（300 秒）一种退出机制，对于 agent 间对话来说
太慢，且无法主动结束无意义循环。

## 退出标识

Agent 在回复文本中嵌入以下标识，Python 侧解析并执行：

| 标识 | 含义 | Python 行为 |
|------|------|------------|
| `[!END]` | 回复后立即结束 | `exit_immediately=True`，设置 `last_activity=0`，下一秒 idle check 触发退出 |
| `[!WAIT]` | 回复后等对方回复 | `exit_immediately=False`，不做处理，走 idle timeout（300 秒）|
| `[!SILENT]` | 不发送回复就结束 | 不调 `send_text`，直接退出 |
| 无标识 | 高召回默认 | 视为 `[!END]`，立即退出 |

### 为什么需要 `[!WAIT]`

在 PM-IC 场景中，PM 发完任务指令后可能需要等 IC 确认（"收到了"、"正在跑"）。
如果 PM 发完就立即结束，IC 的确认消息会触发新的一轮 OneTick 调度，有短暂的重启开销。
`[!WAIT]` 让 PM 留在会话中等待（但不等太久——300 秒 idle timeout 就是上限）。

### 为什么 `[!WAIT]` 不带 N（等待秒数）

300 秒的 idle timeout 已足够作为兜底。增加变长等待（N 秒）会增加 agent 的认知负担（"我该等多久"），
且实际收益有限——`[!WAIT]` 只需表达"我等"，等多久交给系统。

## 提示词引导

在 `_build_context_prefix` 末尾追加以下指导：

```
[退出控制]
如果你想结束本次对话，在回复末尾加上 [!END]。
如果你需要等待对方回复，在回复末尾加上 [!WAIT]。
  加 [!WAIT] 后，会话会等对方回复（最长 300 秒）。
如果你不需要发送消息、直接结束，回复 [!SILENT]（这不礼貌，谨慎使用）。
默认情况下（不写任何标识），会话会在你回复后立即结束。
```

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
