"""Run one piece of model-written code inside the open-task sandbox.

This file is started by ``sandbox.py`` as ``python -I -B _launcher.py <config>``,
inside fresh user, network, PID and mount namespaces when the sandbox is
isolated. It is trusted code: it hides the service's own files, lowers the
resource limits, installs an audit hook that refuses network, process and
out-of-workspace file access, and only then executes ``task.py`` from the
workspace. Nothing here imports the application.

The namespaces are the boundary (no network interface, no view of the API
process); the audit hook is a second line that also covers the "guarded"
mode used in development where namespaces are unavailable.
"""

from __future__ import annotations

import json
import os
import resource
import stat
import sys

MS_BIND = 4096

_BLOCKED_EVENTS = frozenset({
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "os.kill", "os.killpg",
    "pty.spawn", "os.startfile", "webbrowser.open", "ctypes.call_function",
})
_PATH_WRITE_EVENTS = {
    # event -> indexes of the path arguments it carries
    "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "os.chmod": (0,), "os.chown": (0,), "os.truncate": (0,), "os.utime": (0,),
    "os.rename": (0, 1), "os.symlink": (0, 1), "os.link": (0, 1), "shutil.rmtree": (0,), "shutil.move": (0, 1), "shutil.copyfile": (1,),
    "shutil.copytree": (1,), "os.mkfifo": (0,), "os.mknod": (0,),
}
_PATH_LIST_EVENTS = {"os.listdir": 0, "os.scandir": 0, "glob.glob": 0}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _hide(paths: list[str]) -> None:
    """Cover the service's files with empty mounts; fails closed when a path cannot be hidden."""

    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p)
    for path in paths:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue
        if stat.S_ISDIR(mode):
            result = libc.mount(b"tmpfs", path.encode(), b"tmpfs", 0, b"size=64k,mode=0500")
        else:
            result = libc.mount(b"/dev/null", path.encode(), None, MS_BIND, None)
        if result != 0:
            sys.stderr.write(f"sandbox: could not hide a protected path (errno {ctypes.get_errno()})\n")
            raise SystemExit(125)


def _limits(limits: dict[str, int]) -> None:
    for name, value in (
        ("RLIMIT_AS", limits["memory_bytes"]), ("RLIMIT_CPU", limits["cpu_seconds"]), ("RLIMIT_FSIZE", limits["file_bytes"]),
        ("RLIMIT_NOFILE", 256), ("RLIMIT_CORE", 0),
    ):
        resource.setrlimit(getattr(resource, name), (value, value))


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _real(value: object) -> str | None:
    if isinstance(value, int):
        return None  # an already-open descriptor: it was checked when it was opened
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, (str, os.PathLike)):
        return None
    return os.path.realpath(os.fspath(value))


def _install_guard(workspace: str, read_roots: tuple[str, ...]) -> None:
    writable = (workspace, "/dev/null")
    readable = (*read_roots, workspace)

    def refuse(what: str) -> None:
        raise PermissionError(f"sandbox: {what} is not allowed here")

    def hook(event: str, args: tuple[object, ...]) -> None:
        if event.startswith("socket."):
            refuse("network access")
        if event in _BLOCKED_EVENTS:
            refuse("starting processes or calling native code")
        if event == "ctypes.dlopen" and args and args[0] is not None:
            refuse("loading native libraries")
        if event == "open":
            path = _real(args[0]) if args else None
            if path is None:
                return
            mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = any(char in mode for char in "wax+") or bool(flags & _WRITE_FLAGS)
            if writing and not _under(path, writable):
                refuse("writing outside the workspace")
            if not writing and not _under(path, readable):
                refuse("reading outside the workspace")
            return
        if event in _PATH_WRITE_EVENTS:
            for index in _PATH_WRITE_EVENTS[event]:
                path = _real(args[index]) if len(args) > index else None
                if path is not None and not _under(path, writable):
                    refuse("changing files outside the workspace")
            return
        if event in _PATH_LIST_EVENTS:
            index = _PATH_LIST_EVENTS[event]
            path = _real(args[index] if len(args) > index and args[index] is not None else ".")
            if path is not None and not _under(path, readable):
                refuse("listing files outside the workspace")

    sys.addaudithook(hook)


def main() -> None:
    config = json.loads(sys.argv[1])
    workspace = os.path.realpath(config["workspace"])
    if config.get("hide"):
        _hide(list(config["hide"]))
    _limits(config["limits"])
    os.chdir(workspace)
    with open(os.path.join(workspace, "task.py"), encoding="utf-8") as handle:
        source = handle.read()
    code = compile(source, "task.py", "exec")
    _install_guard(workspace, tuple(os.path.realpath(root) for root in config["read_roots"]))
    sys.argv = ["task.py"]
    exec(code, {"__name__": "__main__", "__builtins__": __builtins__})  # noqa: S102 - this is the sandbox's purpose


if __name__ == "__main__":
    main()
