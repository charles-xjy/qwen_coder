"""
sandbox.py - OpenSandbox 会话级单例管理

职责：
  - 维护一个沙箱实例，整个 Agent 会话复用
  - 每次 run() 前增量同步 WORKDIR → 沙箱 /workspace/（只传 mtime 有变化的文件）
  - OpenSandbox 不可用时询问用户是否降级到本地执行
  - 会话结束时调用 close() 销毁沙箱
"""

import asyncio
import os
import subprocess
from datetime import timedelta
from pathlib import Path

from tools import WORKDIR

# ── 配置（从环境变量读取，带默认值）────────────────────────────────────────────

SANDBOX_URL     = os.getenv("OPENSANDBOX_URL",     "10.129.107.145:8080")
SANDBOX_API_KEY = os.getenv("OPENSANDBOX_API_KEY", "opensandbox-2026")
SANDBOX_IMAGE   = os.getenv(
    "OPENSANDBOX_IMAGE",
    "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.0.2",
)
SANDBOX_TIMEOUT_HOURS = int(os.getenv("OPENSANDBOX_TIMEOUT_HOURS", "8"))

# 不同步到沙箱的目录/文件模式
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
_SKIP_EXTS = {".pyc", ".pyo", ".egg-info", ".so", ".dylib", ".dll"}

# 单次上传批次上限（避免一次性传太多文件导致超时）
_UPLOAD_BATCH = 50


# ── 沙箱管理器 ────────────────────────────────────────────────────────────────

class SandboxManager:
    def __init__(self) -> None:
        self._sandbox = None
        self._last_sync_mtimes: dict[str, float] = {}  # 绝对路径 → mtime
        self._local_fallback = False   # 用户同意降级到本地执行
        self._initialized = False      # 沙箱初始化命令（python 软链等）是否已执行
        self._lock = asyncio.Lock()

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    async def run(self, command: str, timeout: int = 30) -> str:
        """
        在沙箱中执行命令，返回 stdout + stderr 合并字符串。
        自动完成文件同步和沙箱健康检查。
        """
        async with self._lock:
            ok = await self._ensure_sandbox()
            if not ok:
                if self._local_fallback:
                    return await self._run_local(command, timeout)
                return "Error: 沙箱不可用，用户已拒绝本地执行。"

            await self._sync_files()

        # 执行命令（不在锁内，允许并发只读）
        try:
            result = await self._sandbox.commands.run(
                command,
                timeout=timedelta(seconds=timeout + 5),
            )
            stdout = "".join(x.text for x in result.logs.stdout)
            stderr = "".join(x.text for x in result.logs.stderr)
            exit_code = result.exit_code

            parts = []
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"[stderr]\n{stderr}")
            if exit_code != 0:
                parts.append(f"[exit code: {exit_code}]")

            return "\n".join(parts) if parts else "（无输出）"
        except Exception as e:
            # 沙箱可能已超时，下次调用时重建
            self._sandbox = None
            self._initialized = False
            return f"Error: 沙箱执行失败（{e}），下次调用将自动重建沙箱。"

    async def close(self) -> None:
        """会话结束时销毁沙箱。"""
        if self._sandbox is not None:
            try:
                await self._sandbox.kill()
            except Exception:
                pass
            self._sandbox = None
            self._initialized = False
            self._last_sync_mtimes.clear()

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    async def _ensure_sandbox(self) -> bool:
        """确保沙箱存活，不可用时触发降级询问。返回 True 表示可以使用沙箱。"""
        if self._local_fallback:
            return False

        if self._sandbox is not None:
            # 快速健康检查
            try:
                alive = await self._sandbox.is_healthy()
                if alive:
                    return True
            except Exception:
                pass
            self._sandbox = None
            self._initialized = False

        # 尝试创建新沙箱
        try:
            from opensandbox import Sandbox
            from opensandbox.config import ConnectionConfig

            config = ConnectionConfig(
                domain=SANDBOX_URL,
                api_key=SANDBOX_API_KEY,
                protocol="http",
                request_timeout=timedelta(seconds=60),
                use_server_proxy=True,
            )
            print("\033[33m[沙箱] 正在启动沙箱...\033[0m")
            self._sandbox = await Sandbox.create(
                SANDBOX_IMAGE,
                entrypoint=["/opt/opensandbox/code-interpreter.sh"],
                timeout=timedelta(hours=SANDBOX_TIMEOUT_HOURS),
                connection_config=config,
            )
            print(f"\033[32m[沙箱] 已就绪 id={self._sandbox.id}\033[0m")
            await self._init_sandbox()
            return True

        except Exception as e:
            print(f"\033[31m[沙箱] 连接失败: {e}\033[0m")
            return await self._ask_local_fallback()

    async def _init_sandbox(self) -> None:
        """沙箱首次启动后的初始化：创建工作目录、设置 python 软链。"""
        if self._initialized:
            return
        cmds = [
            "mkdir -p /workspace",
            # 兼容 python 命令（镜像内只有 python3）
            "ln -sf /usr/bin/python3 /usr/local/bin/python 2>/dev/null || true",
        ]
        for cmd in cmds:
            try:
                await self._sandbox.commands.run(cmd, timeout=timedelta(seconds=10))
            except Exception:
                pass
        self._initialized = True

    async def _sync_files(self) -> None:
        """增量同步 WORKDIR 到沙箱 /workspace/，只上传 mtime 有变化的文件。"""
        changed: list[tuple[str, str]] = []  # (sandbox_path, local_abs_path)

        for local_path in WORKDIR.rglob("*"):
            if not local_path.is_file():
                continue
            # 跳过不需要同步的目录
            if any(skip in local_path.parts for skip in _SKIP_DIRS):
                continue
            # 跳过不需要同步的扩展名
            if local_path.suffix in _SKIP_EXTS:
                continue

            abs_str = str(local_path)
            try:
                mtime = local_path.stat().st_mtime
            except OSError:
                continue

            if self._last_sync_mtimes.get(abs_str) != mtime:
                rel = local_path.relative_to(WORKDIR)
                sandbox_path = f"/workspace/{rel.as_posix()}"
                changed.append((sandbox_path, abs_str, mtime))

        if not changed:
            return

        print(f"\033[33m[沙箱] 同步 {len(changed)} 个文件...\033[0m")

        from opensandbox.models import WriteEntry

        # 分批上传
        for i in range(0, len(changed), _UPLOAD_BATCH):
            batch = changed[i: i + _UPLOAD_BATCH]
            entries = []
            for sandbox_path, abs_str, mtime in batch:
                try:
                    content = Path(abs_str).read_text(encoding="utf-8", errors="replace")
                    entries.append(WriteEntry(path=sandbox_path, data=content, mode=644))
                except Exception:
                    continue
            if entries:
                try:
                    await self._sandbox.files.write_files(entries)
                    # 更新 mtime 记录
                    for sandbox_path, abs_str, mtime in batch:
                        self._last_sync_mtimes[abs_str] = mtime
                except Exception as e:
                    print(f"\033[31m[沙箱] 文件同步失败（批次 {i}）: {e}\033[0m")

    async def _ask_local_fallback(self) -> bool:
        """沙箱不可用时询问用户是否接受本地执行，返回 True 表示同意本地执行。"""
        print(
            "\n\033[33m[警告] OpenSandbox 不可用。\n"
            "本地执行不受沙箱保护，shell 命令将直接运行在宿主机上。\033[0m"
        )
        try:
            answer = input("是否接受本地执行？(yes/no): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "no"

        if answer in ("yes", "y"):
            self._local_fallback = True
            print("\033[33m[沙箱] 已切换到本地执行模式。\033[0m")
            return False  # 返回 False 表示沙箱不可用，调用方走本地路径
        return False

    @staticmethod
    async def _run_local(command: str, timeout: int = 30) -> str:
        """降级：在宿主机本地执行命令（用户已确认同意）。"""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKDIR),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            parts = []
            if stdout:
                parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                parts.append(f"[stderr]\n{stderr.decode('utf-8', errors='replace')}")
            if proc.returncode != 0:
                parts.append(f"[exit code: {proc.returncode}]")
            return "\n".join(parts) if parts else "（无输出）"
        except asyncio.TimeoutError:
            return f"Error: 命令执行超时（{timeout}s）"
        except Exception as e:
            return f"Error: {e}"


# ── 模块级单例 ────────────────────────────────────────────────────────────────

_manager: SandboxManager | None = None


def get_sandbox() -> SandboxManager:
    """返回会话级单例，首次调用时创建。"""
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager


async def close_sandbox() -> None:
    """会话结束时调用，销毁沙箱并重置单例。"""
    global _manager
    if _manager is not None:
        await _manager.close()
        _manager = None
