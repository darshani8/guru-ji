"""A workspace where the open-task agent's Python runs, away from the API process.

Each task gets a private directory (``data/`` for the records its tools
returned, ``outputs/`` for the files it makes) that is deleted when the task
ends. Code runs in a child interpreter started through ``_launcher.py``:

* ``isolated`` (the default, and the only mode production accepts) starts the
  child in new user, network, PID and mount namespaces with ``unshare``: it has
  no network interface, cannot see the API process, and the service's own
  files (sources, configuration, local data) are covered by empty mounts.
* ``guarded`` runs the child without namespaces, for development machines
  where ``unshare`` is not permitted. Only the launcher's audit hook and the
  scrubbed environment stand between the code and the host, so it is refused
  in production.
* ``auto`` picks ``isolated`` when the host allows it and ``guarded`` otherwise.

In every mode the child gets a scrubbed environment (no API keys or cloud
credentials), CPU, memory and file-size limits, and a wall-clock timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import site
import stat
import subprocess  # noqa: S404 - the sandbox exists to start a constrained child interpreter
import sys
import sysconfig
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..actions.files import FORMAT_CONTENT_TYPES

logger = logging.getLogger("saffron.open_task.sandbox")

SANDBOX_MODES = ("isolated", "auto", "guarded")
LAUNCHER = Path(__file__).with_name("_launcher.py")
UNSHARE_ARGS = ("-r", "-n", "-p", "-f", "-m", "--mount-proc")
# What a task may hand back: the report formats the download route serves.
OUTPUT_TYPES = FORMAT_CONTENT_TYPES
# Libraries worth telling the model about when they are importable.
KNOWN_LIBRARIES = {
    "openpyxl": "Excel workbooks (sheets, formulas, number formats, conditional formatting, charts)",
    "pptx": "PowerPoint decks (python-pptx: slides, text, tables, pictures, native charts)",
    "docx": "Word documents (python-docx: headings, paragraphs, tables, pictures)",
    "matplotlib": "charts saved as PNG (use the Agg backend)",
    "numpy": "numeric arrays",
    "pandas": "data frames",
    "PIL": "images (Pillow)",
    "reportlab": "PDF documents",
    "pypdf": "reading and merging PDFs",
}
_READ_ROOTS = ("/usr", "/lib", "/lib64", "/lib32", "/etc", "/sys", "/dev/null", "/dev/urandom", "/dev/random", "/dev/zero", "/proc/self", "/proc/cpuinfo", "/proc/meminfo")


class SandboxUnavailable(RuntimeError):
    """The host cannot provide the sandbox mode that was configured."""


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True, slots=True)
class OutputFile:
    name: str
    extension: str
    content_type: str
    content: bytes


@dataclass(slots=True)
class SandboxConfig:
    mode: str = "isolated"
    run_timeout_seconds: float = 120.0
    memory_mb: int = 2048
    max_file_mb: int = 50
    max_output_chars: int = 20_000
    max_output_files: int = 10
    base_dir: str | None = None
    # Directories and files the child must never see. The service's own tree
    # and the home directory are added automatically.
    hidden_paths: tuple[str, ...] = ()
    unshare_path: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in SANDBOX_MODES:
            raise ValueError(f"sandbox mode must be one of {', '.join(SANDBOX_MODES)}")
        if self.run_timeout_seconds <= 0 or self.memory_mb < 256 or self.max_file_mb <= 0 or self.max_output_files <= 0:
            raise ValueError("sandbox limits must be positive (memory at least 256 MB)")


_NAMESPACES_OK: dict[str, bool] = {}


def namespaces_available(unshare: str | None = None) -> bool:
    """Whether this host lets an unprivileged process create the sandbox's namespaces (checked once per binary)."""

    binary = unshare or shutil.which("unshare")
    if not binary:
        return False
    if binary not in _NAMESPACES_OK:
        try:
            completed = subprocess.run([binary, *UNSHARE_ARGS, "--", "true"], capture_output=True, timeout=10, check=False)  # noqa: S603
            _NAMESPACES_OK[binary] = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _NAMESPACES_OK[binary] = False
    return _NAMESPACES_OK[binary]


def available_libraries() -> dict[str, str]:
    return {name: purpose for name, purpose in KNOWN_LIBRARIES.items() if importlib.util.find_spec(name) is not None}


def _library_roots() -> tuple[str, ...]:
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        with contextlib.suppress(KeyError):
            roots.add(sysconfig.get_paths()[key])
    roots.update(site.getsitepackages())
    return tuple(sorted(os.path.realpath(root) for root in roots if root))


def _service_root() -> Path:
    # apps/api/app/open_task/sandbox.py -> the repository (or /app in the image)
    return Path(__file__).resolve().parents[4]


def _contains(parent: str, child: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


_SYSTEM_DIRS = frozenset({"/", "/usr", "/etc", "/proc", "/dev", "/sys", "/lib", "/lib64", "/bin", "/sbin", "/tmp", "/var", "/home"})


def _hide_around(path: str, keep: tuple[str, ...], depth: int = 0) -> list[str]:
    """``path`` itself, or, when the interpreter lives inside it, its other children."""

    real = os.path.realpath(path)
    if real in _SYSTEM_DIRS or any(_contains(kept, real) for kept in keep):
        return []
    if not any(_contains(real, kept) for kept in keep):
        return [real] if os.path.lexists(real) else []
    if depth > 6 or not os.path.isdir(real):
        return []
    hidden: list[str] = []
    with contextlib.suppress(OSError):
        for child in Path(real).iterdir():
            hidden.extend(_hide_around(str(child), keep, depth + 1))
    return hidden


def default_hidden_paths(extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The service tree (except the interpreter it runs on), the home directory, and anything configured."""

    keep = (*_library_roots(), os.path.realpath(sys.executable))
    candidates = [str(_service_root()), os.path.expanduser("~"), *extra]
    hidden: list[str] = []
    for candidate in candidates:
        if candidate:
            hidden.extend(_hide_around(candidate, keep))
    return tuple(dict.fromkeys(hidden))


class Workspace:
    """One task's private directory and the runs made in it."""

    def __init__(self, config: SandboxConfig, *, isolated: bool, unshare: str | None) -> None:
        self.config = config
        self.isolated = isolated
        self._unshare = unshare
        base = config.base_dir or tempfile.gettempdir()
        self.root = Path(tempfile.mkdtemp(prefix="saffron-task-", dir=base)).resolve()
        for name in ("data", "outputs", "tmp", ".config", ".cache"):
            (self.root / name).mkdir()
        os.chmod(self.root, 0o700)
        self.runs = 0
        hidden = [path for path in default_hidden_paths(config.hidden_paths) if not _contains(path, str(self.root)) and not _contains(str(self.root), path)]
        self._hidden = tuple(hidden) if isolated else ()

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def outputs_dir(self) -> Path:
        return self.root / "outputs"

    def write_data(self, name: str, content: bytes) -> str:
        """Store a tool result for the task's code; returns the path relative to the workspace."""

        safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)[:120].lstrip(".") or "data"
        target = self.data_dir / safe
        target.write_bytes(content)
        return f"data/{safe}"

    def _environment(self) -> dict[str, str]:
        root = str(self.root)
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": root, "TMPDIR": f"{root}/tmp", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "MPLBACKEND": "Agg", "MPLCONFIGDIR": f"{root}/.config/matplotlib", "XDG_CACHE_HOME": f"{root}/.cache", "XDG_CONFIG_HOME": f"{root}/.config",
            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        }

    def _command(self) -> list[str]:
        config = {
            "workspace": str(self.root),
            "hide": list(self._hidden),
            "read_roots": [*_library_roots(), *_READ_ROOTS],
            "limits": {
                "memory_bytes": self.config.memory_mb * 1024 * 1024,
                "cpu_seconds": int(self.config.run_timeout_seconds) + 5,
                "file_bytes": self.config.max_file_mb * 1024 * 1024,
            },
        }
        launch = [sys.executable, "-I", "-B", str(LAUNCHER), json.dumps(config)]
        if self.isolated:
            return [str(self._unshare), *UNSHARE_ARGS, "--", *launch]
        return launch

    async def run(self, code: str, *, timeout_seconds: float | None = None) -> RunResult:
        self.runs += 1
        (self.root / "task.py").write_text(code, encoding="utf-8")
        timeout = min(timeout_seconds or self.config.run_timeout_seconds, self.config.run_timeout_seconds)
        process = await asyncio.create_subprocess_exec(
            *self._command(), cwd=str(self.root), env=self._environment(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        cap = self.config.max_output_chars
        stdout_task = asyncio.create_task(_drain(process.stdout, cap))
        stderr_task = asyncio.create_task(_drain(process.stderr, cap))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        stdout, cut_out = await stdout_task
        stderr, cut_err = await stderr_task
        return RunResult(process.returncode, stdout, stderr, timed_out=timed_out, truncated=cut_out or cut_err)

    def list_outputs(self) -> list[dict[str, object]]:
        return [{"name": item.name, "size_bytes": item.stat().st_size} for item in sorted(self.outputs_dir.iterdir()) if item.is_file() and not item.is_symlink()]

    def collect_outputs(self) -> tuple[list[OutputFile], list[str]]:
        """The deliverables in ``outputs/``; returns the files and the reasons any were left out."""

        files: list[OutputFile] = []
        skipped: list[str] = []
        limit = self.config.max_file_mb * 1024 * 1024
        for item in sorted(self.outputs_dir.iterdir()):
            info = item.lstat()
            if not stat.S_ISREG(info.st_mode):
                skipped.append(f"{item.name}: not a regular file")
                continue
            extension = item.suffix.lower().lstrip(".")
            if extension not in OUTPUT_TYPES:
                skipped.append(f"{item.name}: .{extension or '?'} files are not delivered")
                continue
            if info.st_size == 0 or info.st_size > limit:
                skipped.append(f"{item.name}: empty or larger than {self.config.max_file_mb} MB")
                continue
            if len(files) >= self.config.max_output_files:
                skipped.append(f"{item.name}: more than {self.config.max_output_files} files")
                continue
            descriptor = os.open(item, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                content = handle.read(limit + 1)
            files.append(OutputFile(item.name, extension, OUTPUT_TYPES[extension], content))
        return files, skipped

    def close(self) -> None:
        # The data files hold institutional records: they never outlive the task.
        shutil.rmtree(self.root, ignore_errors=True)


async def _drain(stream: asyncio.StreamReader | None, cap: int) -> tuple[str, bool]:
    """Read a pipe to the end, keeping only the first ``cap`` characters so a chatty task cannot exhaust memory."""

    if stream is None:
        return "", False
    kept = bytearray()
    truncated = False
    while chunk := await stream.read(65536):
        room = cap * 4 - len(kept)
        if room > 0:
            kept.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True
    text = kept.decode("utf-8", errors="replace")
    if len(text) > cap:
        text, truncated = text[:cap], True
    return text, truncated


@dataclass(slots=True)
class Sandbox:
    config: SandboxConfig = field(default_factory=SandboxConfig)

    def resolve_isolation(self) -> tuple[bool, str | None]:
        unshare = self.config.unshare_path or shutil.which("unshare")
        available = namespaces_available(unshare)
        if self.config.mode == "isolated" and not available:
            raise SandboxUnavailable("the isolated code sandbox needs unprivileged user namespaces (unshare), which this host does not allow")
        if self.config.mode == "guarded" or not available:
            return False, None
        return True, unshare

    def open(self) -> Workspace:
        isolated, unshare = self.resolve_isolation()
        if not isolated:
            logger.warning("open-task sandbox is running in guarded mode (no namespaces); use it for development only")
        return Workspace(self.config, isolated=isolated, unshare=unshare)


__all__ = [
    "OUTPUT_TYPES",
    "SANDBOX_MODES",
    "OutputFile",
    "RunResult",
    "Sandbox",
    "SandboxConfig",
    "SandboxUnavailable",
    "Workspace",
    "available_libraries",
    "default_hidden_paths",
    "namespaces_available",
]
