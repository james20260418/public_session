#!/usr/bin/env python3
"""
public_session.py — 公共会话主循环（仓库已转移至 DanisTeng）

1 秒固定心跳，OneTick 为唯一执行接口。

用法：public_session.py <config.json>

约定：
  - 飞书 APP_ID/APP_SECRET 走环境变量，不写进文件
  - 此脚本本身不包含任何用户/会话/环境特化信息

退出机制：
  - 主循环每秒检查 stop 文件，存在时 cleanup 后退出
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

from config import Config, CachedTokenProvider, load as load_config
from message_manager import MessageManager
from scheduler import pick_candidate, Candidate
from single_chat_manager.single_chat_manager import _load_last_processed
from single_chat_manager.v2 import SingleChatManagerV2

# ── 常量 ────────────────────────────────────────────────────────────────

HEARTBEAT_SECONDS = 1
HKT = timezone(timedelta(hours=8))


# ── 日志 ────────────────────────────────────────────────────────────────

def log(log_path, msg):
    """追加日志到指定文件"""
    ts = datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if log_path:
        od = os.path.dirname(log_path)
        if od:
            os.makedirs(od, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(line + "\n")


# ── OneTick ─────────────────────────────────────────────────────────────

def one_tick(config: Config, mgr: MessageManager, token_provider: CachedTokenProvider):
    """单次 tick 执行

    检查是否有 sender 等待对话。如有，启动 SingleChatManagerV2 阻塞处理。
    处理完成后回到下一 tick。

    Args:
        config: 配置
        mgr: MessageManager 实例
        token_provider: 缓存 token 提供者
    """
    log_path = config.log_file
    ws_root = os.path.dirname(os.path.expanduser(config.log_file)) if config.log_file else "."
    lp = _load_last_processed(config)
    candidate = pick_candidate(mgr, lp)
    if candidate is None:
        return

    log(log_path, f"🎯 选中 {candidate.sender_name}（等待 {candidate.wait_seconds:.0f}s）")

    scm = SingleChatManagerV2(
        config=config,
        message_manager=mgr,
        ws_root=ws_root,
        token_provider=token_provider,
        log_file=config.log_file,
        stop_file=os.path.expanduser(config.stop_file),
    )
    result = scm.run(candidate)

    suffix = "，超时" if result.timed_out else ""
    log(log_path, f"📞 会话结束: {candidate.sender_name} "
                  f"({result.message_count} 条消息{suffix})")


# ── Cleanup ──────────────────────────────────────────────────────────────

def cleanup(config: Config, mgr: MessageManager):
    """进程退出收尾

    清理资源、关闭连接、移除 stop 文件。

    Args:
        config: 配置
        mgr: MessageManager 实例
    """
    log_path = config.log_file
    log(log_path, "🧹  Cleanup: shutting down")

    mgr.stop()

    # ── 清理 stop 文件，下次 run 不会立刻停止 ──
    stop_file = os.path.expanduser(config.stop_file)
    if stop_file and os.path.exists(stop_file):
        os.remove(stop_file)
        log(log_path, f"🗑️  Removed stop file: {stop_file}")

    log(log_path, "✅  Cleanup done")


# ── 主循环 ──────────────────────────────────────────────────────────────

def run_loop(config: Config):
    """主循环入口

    1 秒固定心跳，检测 stop 文件时执行 cleanup 后退出。

    Args:
        config: 配置
    """
    log_path = config.log_file
    stop_file = os.path.expanduser(config.stop_file)

    # 启动 MessageManager（WS 后台线程）
    mgr = MessageManager(
        app_id=config.resolved_app_id,
        app_secret=config.resolved_app_secret,
        mark_get_on_receive=False,  # typing/done 由 SingleChatManager 管理
        log_file=config.log_file,
        file_storage_dir=config.file_storage_dir,
    )
    mgr.start()

    token_provider = config.new_token_provider()

    log(log_path, "🚀  Public session started")

    while True:
        tick_start = time.time()

        # 退出检查
        if stop_file and os.path.exists(stop_file):
            log(log_path, "🛑  Stop file detected, exiting")
            break

        # 执行 OneTick
        one_tick(config, mgr, token_provider)

        # 心跳等待（补足到 1 秒）
        elapsed = time.time() - tick_start
        sleep_sec = max(0, HEARTBEAT_SECONDS - elapsed)
        time.sleep(sleep_sec)

    cleanup(config, mgr)


# ── 入口 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <config.json>", file=sys.stderr)
        sys.exit(1)

    config = load_config(sys.argv[1])
    run_loop(config)
