#!/usr/bin/env python3
"""
send_file.py — 通过飞书 bot 发送文件给指定用户。

独立 main 入口，供 OpenClaw agent 在对话中调用。

用法：
    python -m util.send_file <open_id> <file_path>

凭证共享 public_session 的环境变量：
    PUBLIC_FEISHU_APP_ID / PUBLIC_FEISHU_APP_SECRET

流程：
  1. 获取 tenant_access_token（复用 util.feishu）
  2. 上传文件到飞书，获取 file_key（POST multipart/file）
  3. 发送文件消息给指定用户（复用 util.feishu._request）
"""

import json
import os
import sys
import urllib.request
import urllib.error

from util.feishu import API_BASE, get_token, _request


# ── 支持的格式 ──────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = frozenset({
    # 文档类
    ".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".csv", ".html", ".htm", ".xml", ".json",
    # 图片类（走 file 类型上传，不走 image 消息）
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
    # 音视频类
    ".mp3", ".wav", ".aac", ".flac", ".ogg",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm",
    # 压缩包
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    # 代码
    ".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".sh", ".bat", ".ps1", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".sql", ".proto",
    # 其他常见格式
    ".log",
})

# 飞书 file_type 参数映射
_FILE_TYPE_MAP = {
    ".pdf": "pdf",
    ".doc": "doc", ".docx": "doc",
    ".xls": "xls", ".xlsx": "xls",
    ".ppt": "ppt", ".pptx": "ppt",
    ".mp4": "mp4",
    ".opus": "opus",
}

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB


# ── 文件上传（multipart/form-data）────────────────────────────────────

def _resolve_file_type(file_path: str) -> str:
    """根据文件扩展名获取飞书 file_type 参数。非标准格式统一用 stream。"""
    ext = os.path.splitext(file_path)[1].lower()
    return _FILE_TYPE_MAP.get(ext, "stream")


def _build_multipart(file_path: str) -> tuple[bytes, str]:
    """构造飞书上传文件的 multipart body。

    Returns:
        (body_bytes, content_type_header)
    """
    file_name = os.path.basename(file_path)
    file_type = _resolve_file_type(file_path)
    boundary = "----FormBoundary7MA4YWxkTrZu0gW"

    with open(file_path, "rb") as f:
        file_data = f.read()

    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file_type"\r\n\r\n'
        f"{file_type}\r\n".encode(),

        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file_name"\r\n\r\n'
        f"{file_name}\r\n".encode(),

        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode(),
        file_data,
        b"\r\n",

        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_file(token: str, file_path: str) -> dict:
    """上传文件到飞书，获取 file_key。"""
    if not os.path.isfile(file_path):
        return {"code": -1, "msg": f"文件不存在: {file_path}"}

    file_name = os.path.basename(file_path)
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return {"code": -1, "msg": f"不支持的文件格式: {ext}"}

    file_size = os.path.getsize(file_path)
    if file_size > MAX_FILE_SIZE:
        return {"code": -1, "msg": f"文件过大 ({file_size // 1024 // 1024}MB > 100MB)"}

    body, content_type = _build_multipart(file_path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }

    req = urllib.request.Request(
        f"{API_BASE}/im/v1/files", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode(errors="replace")[:300]
        except Exception:
            detail = str(e)
        return {"code": -1, "msg": f"上传 HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"code": -1, "msg": str(e)[:200]}

    if result.get("code") != 0:
        return {"code": result["code"], "msg": result.get("msg", str(result))}

    file_key = result.get("data", {}).get("file_key", "")
    if not file_key:
        return {"code": -1, "msg": "上传成功但未返回 file_key"}

    return {"code": 0, "file_key": file_key, "file_name": file_name}


# ── 发送文件消息（复用 _request）─────────────────────────────────────

def send_file_message(token: str, open_id: str, file_key: str) -> dict:
    """通过 file_key 发送文件消息给指定用户。"""
    content = json.dumps({"file_key": file_key})
    return _request(
        "/im/v1/messages?receive_id_type=open_id",
        token=token,
        method="POST",
        body={"receive_id": open_id, "msg_type": "file", "content": content},
    )


# ── 凭证解析（复用 config.Config）─────────────────────────────────────

def _get_credentials() -> tuple[str, str]:
    """从环境变量获取飞书凭证（与 public_session 共用）。"""
    app_id = os.environ.get("PUBLIC_FEISHU_APP_ID", "")
    app_secret = os.environ.get("PUBLIC_FEISHU_APP_SECRET", "")
    return app_id, app_secret


# ── main ────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        for ext in sorted(SUPPORTED_EXTENSIONS):
            print(ext)
        return

    if len(sys.argv) < 3:
        print(f"用法: python -m util.send_file <open_id> <file_path>",
              file=sys.stderr)
        print(f"       python -m util.send_file --list", file=sys.stderr)
        sys.exit(1)

    open_id, file_path = sys.argv[1], sys.argv[2]

    app_id, app_secret = _get_credentials()
    if not app_id or not app_secret:
        print("ERROR: 请设置 PUBLIC_FEISHU_APP_ID / PUBLIC_FEISHU_APP_SECRET",
              file=sys.stderr)
        sys.exit(1)

    token = get_token(app_id, app_secret)
    if not token:
        print("ERROR: 获取飞书 token 失败", file=sys.stderr)
        sys.exit(1)

    result = upload_file(token, file_path)
    if result.get("code") != 0:
        print(f"ERROR: {result.get('msg', '上传失败')}", file=sys.stderr)
        sys.exit(1)

    send_r = send_file_message(token, open_id, result["file_key"])
    if send_r.get("code") != 0:
        print(f"ERROR: 发送失败: {send_r.get('msg', '')}", file=sys.stderr)
        sys.exit(1)

    # stdout 输出 JSON 供程序化消费
    print(json.dumps({
        "status": "ok",
        "file_name": result["file_name"],
        "file_key": result["file_key"],
    }))


if __name__ == "__main__":
    main()
