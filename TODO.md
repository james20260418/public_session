# Public Session — 工作现状总结 & 待办事项

> 写于 2026-06-01，暂停开发前的状态快照。

---

## 一、已实现的能力 ✅

### 核心架构
- **1 秒固定心跳主循环**（`public_session.py`）— OneTick 为唯一执行接口
- **飞书 WS 长连接消息接收**（`message_manager.py`）— WS 线程持续收消息，线程安全消息表
- **最久等待优先调度**（`scheduler.py`）— 纯函数 pick_candidate，从所有 sender 中选等待最久的
- **SingleChatManager**（`single_chat_manager.py`）— 一次完整会话的执行器，阻塞运行

### 对话能力
- **OpenClaw agent 集成**（`util/openclaw.py`）— 增量会话，`public-session-{open_id}` 上下文累积
- **Debounce 机制**（1s）— 用户连续发消息时等待输入结束再处理
- **Typing / Done 表情** — 处理中打 Typing，完成后换 Done
- **退出 SLEEP 表情** 💤 — 会话结束时给最后一条消息打 SLEEP
- **退出标识解析**（`util/exit_pragma.py`）— `NO_REPLY`（静默退出）、`[!WAIT]`（等待回复）
- **语气指导** — Context prefix 中内置自然有礼貌、简短不枚举、不知道就直说
- **超时退出**（300s 无回复自动结束）

### 记忆系统
- **PPPC（逐文件原始对话存储）** — 每次会话结束写入 `memory/public-session/{open_id}/{timestamp}.md`
- **前情提要** — 新会话 init 时读取最近的 PPPC 文件（~1000 chars）作为对话背景
- **原生日记** — 会话结束调主 session 生成摘要走 OpenClaw 原生日记路径

### 文件能力
- **接收文件**（JPEG/PDF/MOV 等）— WS 收到 non-text 消息自动下载到本地，不入对话表
- **发送文件**（`util/send_file.py`）— 支持 30+ 格式，agent 可主动发文件给用户

### MD 渲染（部分）
- 消息走 `msg_type: post` + `tag: md` 格式发送
- ⚠️ 飞书 REST API 不解析 `tag: md`（降级为纯 text），飞书 SDK 路径才能正确渲染

---

## 二、三个主要待办 ⚠️

### TODO 1：SCM 主 session 提示词每次都带 1000+ 字背景

**问题**：每次 batch 都往 prompt 里塞 `[对话背景]\n{1000 char PPPC}`，哪怕对话已经进行到第三轮。

**原因**：
- OpenClaw session (`public-session-{open_id}`) 是增量累积的
- `_context_prefix` 只在首次 batch 时构建，但 `_process_batch` 每轮都把它拼进 prompt
- 第一轮：`[对话背景]{1000字}\n\n[用户新消息]{消息}`
- 第二轮：`[对话背景]{1000字}\n\n[用户新消息]{消息}` ← 同样 1000 字又喂了一遍

**修法方向**：
- 选项 A：只在首次 batch 带 `[对话背景]`，后续 batch 只发用户消息本身
- 选项 B：确认 OpenClaw session 是否会保留前几条消息的上下文；如果会，第一轮的对话背景在第一轮已经给过了，后续不需要再给

### TODO 2：自主退出能力有限

**问题**：只会因超时（300s）或 stop 文件退出，不会智能判断对话是否应该结束。

**当前退出路径**：
- 超时 300s 无回复 → 打 SLEEP 💤
- stop 文件 → 强制终止
- agent 回复 `NO_REPLY` → silent exit
- agent 回复带 `[!WAIT]` → 等待对方继续

**缺失的智能判断**：
- 不知道对方是否已离线（只能傻等 300s）
- 对方说"我先走了"但没带退出标识 → 不会触发结束
- 不会识别对话自然结束的时机（话题已完、对方无意愿继续等）
- 无法识别负面信号（对方不高兴、拒绝交流）

**修法方向**：
- 在 agent prompt 中加会话终结判断指导
- 让 agent 能在合适时机主动输出结束标识
- 增加"离线检测"（如 30s 内无 Get 表情响应）

### TODO 3：尚无可主动驱动任务的能力

**问题**：当前系统纯被动响应 — 只有对方发消息才会动，处理完就结束。

**现状**：
- OneTick 只做 checked: `有未处理消息？→ 处理 → 结束`
- SCM 只管"收消息 → 回消息 → 等待 → 超时退出"
- 没有"主动找某个用户聊天"的能力
- 没有"定时检查某用户状态"的能力
- 没有"下发任务并跟踪完成状态"的能力

**需要支持的主动能力**：
- **任务队列持久化**：`tasks/{open_id}.json`
- **任务调度器**：SCM 或 OneTick 每轮检查队列，有任务时主动发起对话
- **任务状态回写**：对话结束后更新任务状态（完成/失败/待确认）
- **触发方式**：cron 定时触发 / 其他系统调用 API

---

## 三、技术债务

| 项 | 严重度 | 说明 |
|---|---|---|
| MD 渲染降级 | 中 | `tag: md` 在 REST API 下不生效，需改走 SDK 或用 tag: text + style |
| PPPC 写日记时主 session 看不到原文 | 低 | finalize 调主 session 写日记时，PPPC 文件在对面的子目录，主 session 无权限读取 |
| `util/config.py` 无人引用 | 低 | 残留文件（旧架构的） |
| Agent prompt 过长 | 中 | 每次 batch 都拼完整上下文 prefix，token 浪费 |

---

## 四、文件结构

```
/james_pm/public_session/
├── public_session.py                  # 主循环（1s 心跳，OneTick 调度）
├── config.py                          # 配置数据类 + CachedTokenProvider
├── config.json                        # 运行配置
├── scheduler.py                       # 最久等待优先调度器
├── message_manager.py                 # WS 消息接收 + 消息表 + 文件下载
├── single_chat_manager/
│   ├── single_chat_manager.py         # 完整会话生命周期管理
│   └── __init__.py
├── util/
│   ├── __init__.py
│   ├── feishu.py                      # 飞书 REST API 封装
│   ├── config.py                      # （残留）旧架构配置
│   ├── openclaw.py                    # OpenClaw agent CLI 封装
│   ├── exit_pragma.py                 # 退出标识解析（NO_REPLY / [!WAIT]）
│   ├── send_file.py                   # 文件发送工具
│   └── temp_session.py                # 临时 OpenClaw session 工具
├── test_*.py                          # 测试文件
├── state/                             # 运行时状态（last_processed.json, logs）
├── received_files/                    # 收到的文件
├── run.sh / stop.sh                   # 启动/停止脚本
├── legacy/                            # 旧架构残留
└── TODO.md                            ← 本文档
```

---

## 五、已合并 PR 列表

| PR | 内容 | 日期 |
|---|---|---|
| #9 | 新框架骨架（1s 心跳，空 one_tick，Config 数据类） | 5/14 |
| #10 | OneTick 消息处理（msg_id 边界，CachedTokenProvider，Get/Done 表情） | 5/14 |
| #11 | Scheduler + NameResolver + WS 消息处理修复 | 5/15 |
| #13 | SingleChatManager 传声筒 | 5/15 |
| #14 | Typing/Done reaction + stop-file 退出 | 5/15 |
| #15 | OpenClaw API 集成（generate_reply, _process_batch） | 5/16 |
| #16 | run_temp_session + 记忆设计（PPPC + 原生日记） | 5/17 |
| #20 | Public Session 记忆系统（PPPC 逐文件存储 + 前情提要） | 5/20 |
| #22 | 文件接收能力 | 5/21 |
| #24 | 语气 instruction | 5/31 |
| #25 | MD 渲染（post + md tag，但 REST API 不解析） | 5/31 |
| #26 | 退出 SLEEP 表情 + idle timeout 300s | 5/31 |
