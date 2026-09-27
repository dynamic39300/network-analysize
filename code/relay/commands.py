"""Subprocess boundary for fixed diagnostics and mature repairs; dynamic jobs use jobs.py.

Keep this module independent of the UI and commercial services. Tests inject a
runner with the same (argv, timeout) signature; no shell is involved.
"""
from dataclasses import dataclass
import os
import subprocess


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False

    @property
    def ok(self):
        return self.returncode == 0 and not self.timed_out


class CommandError(RuntimeError):
    pass


def run_command(argv, timeout=10):
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(v, str) for v in argv):
        raise ValueError("Commands must be nonempty argv sequences")
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
            env={**os.environ, "LC_ALL": "C"}, shell=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        return CommandResult(result.stdout, result.stderr, result.returncode)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return CommandResult(stdout, "command timed out", -1, True)
    except OSError as exc:
        return CommandResult("", str(exc), 127)


def checked(runner, argv, timeout=10):
    result = runner(list(argv), timeout=timeout)
    if not isinstance(result, CommandResult):
        raise CommandError("命令执行器返回了无效结果")
    if not result.ok:
        reason = "超时" if result.timed_out else f"退出码 {result.returncode}"
        raise CommandError(f"{os.path.basename(argv[0])} 执行未完成（{reason}）")
    return result.stdout.strip()
