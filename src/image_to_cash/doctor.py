from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from .artifacts import source_identity
from .config import Settings
from .extraction import api_key


def workspace_matches_title(title: str, expected: Path) -> bool:
    prefix = "Fakturama - "
    return (
        title.startswith(prefix)
        and Path(title[len(prefix) :].strip()).resolve() == expected.resolve()
    )


def fakturama_path(settings: Settings) -> Path | None:
    configured = os.getenv("ITC_FAKTURAMA")
    candidates = [Path(configured)] if configured else []
    candidates += [
        Path(os.getenv("PROGRAMFILES", "C:/Program Files")) / "Fakturama2/Fakturama.exe",
        settings.runtime / "tools/Fakturama/Fakturama.exe",
        Path(os.getenv("PROGRAMFILES", "C:/Program Files")) / "Fakturama/Fakturama.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def java_candidates(settings: Settings) -> list[Path]:
    configured = os.getenv("ITC_JAVA")
    if configured:
        return [Path(configured)]
    return sorted((settings.runtime / "tools/java").glob("*/bin/javaw.exe"))


def java_runtime(settings: Settings) -> dict:
    """Report the Java runtime `launch` would pass to Fakturama, from `java -version` itself."""
    candidates = java_candidates(settings)
    if len(candidates) != 1:
        return {"selected": None, "note": "Fakturama uses its bundled runtime"}
    console = candidates[0].with_name("java.exe")
    try:
        result = subprocess.run(
            [str(console), "-version"], capture_output=True, text=True, timeout=30, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"selected": str(candidates[0]), "error": type(exc).__name__}
    return {"selected": str(candidates[0]), "version_output": result.stderr.strip().splitlines()}


def desktop_unlocked() -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    handle = user32.OpenInputDesktop(0, False, 0x0100)
    if handle:
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        user32.CloseDesktop(handle)
        return True
    return False


def observed_windows() -> list[dict]:
    if sys.platform != "win32":
        return []
    import win32gui
    import win32process

    windows = []

    def inspect(handle, _):
        title = win32gui.GetWindowText(handle)
        if win32gui.IsWindowVisible(handle) and title.startswith("Fakturama -"):
            windows.append(
                {"title": title, "pid": win32process.GetWindowThreadProcessId(handle)[1]}
            )

    win32gui.EnumWindows(inspect, None)
    return windows


def diagnose(settings: Settings, *, brief: bool = False, workspace: str | None = None) -> dict:
    import platform

    from .extraction import DEFAULT_GEMINI_MODEL
    from .ocr import executable

    app = fakturama_path(settings)
    windows = observed_windows()
    result = {
        "platform": platform.system(),
        "interactive_desktop": desktop_unlocked(),
        "fakturama_executable": str(app) if app else None,
        "api_key_configured": bool(api_key()),
        "gemini_key_configured": bool(api_key("GEMINI_API_KEY")),
        "fakturama_windows": windows,
    }
    if brief:
        return result
    if workspace is not None:
        directory = settings.workspace_path(workspace)
        result["workspace"] = {
            "name": workspace,
            "directory": str(directory),
            "initialized": (directory / "data").is_dir(),
            "connected": len(windows) == 1
            and workspace_matches_title(windows[0]["title"], directory / "data"),
        }
    versions = {}
    for package in (
        "pydantic",
        "sqlalchemy",
        "fastapi",
        "openai",
        "pywinauto",
        "pillow",
        "rapidocr",
        "onnxruntime",
    ):
        try:
            versions[package] = version(package)
        except Exception:
            versions[package] = "not installed"
    result.update(
        python=platform.python_version(),
        dependencies=versions,
        runtime=str(settings.runtime),
        database=settings.db_url.split(":", 1)[0],
        openai_model=settings.model,
        vision_provider=api_key("ITC_PROVIDER") or "auto",
        gemini_model=api_key("ITC_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        extraction_ocr_engine=settings.ocr_engine,
        email_discrepancy_policy=settings.email_discrepancy_policy,
        source_identity=source_identity(),
        java=java_runtime(settings),
        fakturama_builds=[
            path.stem for path in (app.parent / "plugins").glob("com.sebulli.fakturama.rcp_*.jar")
        ]
        if app
        else [],
    )
    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = executable(settings)
        result["ocr"] = {
            "executable": executable(settings),
            "version": str(pytesseract.get_tesseract_version()),
            "languages": pytesseract.get_languages(),
        }
    except Exception as exc:
        result["ocr"] = {"error": str(exc)}
    import importlib.util

    spec = importlib.util.find_spec("rapidocr")
    model_files = (
        sorted((Path(spec.origin).parent / "models").glob("*.onnx")) if spec and spec.origin else []
    )
    result["rapidocr_models"] = [
        {"file": path.name, "bytes": path.stat().st_size} for path in model_files
    ]
    result["office_integration"] = next(
        (
            str(path)
            for path in [
                Path(os.getenv("PROGRAMFILES", "C:/Program Files"))
                / "LibreOffice/program/soffice.exe",
                settings.runtime / "tools/LibreOffice/program/soffice.exe",
            ]
            if path.exists()
        ),
        None,
    )
    try:
        settings.prepare()
        check = settings.runtime / ".write-check"
        check.write_text("ready", encoding="utf-8")
        check.unlink()
        result["runtime_writable"] = True
    except OSError:
        result["runtime_writable"] = False
    return result
