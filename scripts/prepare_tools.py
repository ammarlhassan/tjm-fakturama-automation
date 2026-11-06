"""Download portable tools into local application data; never change system installations."""

import hashlib
import json
import os
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(os.environ["LOCALAPPDATA"]) / "TJM-ImageToCash" / "tools"


def download(url: str, path: Path) -> Path:
    if path.exists() and path.stat().st_size > 0:
        return path
    print(f"Downloading {path.name}", flush=True)
    partial = path.with_suffix(path.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "TJM-ImageToCash-setup"})
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            target.write(chunk)
    partial.replace(path)
    print(f"Downloaded {path.name}: {path.stat().st_size:,} bytes", flush=True)
    return path


def release(repo: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"User-Agent": "TJM-ImageToCash-setup"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


if __name__ == "__main__":
    import sys

    ROOT.mkdir(parents=True, exist_ok=True)
    task = sys.argv[1]
    if task == "lessmsi":
        data = release("activescott/lessmsi")
        asset = next(a for a in data["assets"] if a["name"].endswith(".zip"))
        archive = download(asset["browser_download_url"], ROOT / "lessmsi.zip")
        with zipfile.ZipFile(archive) as package:
            package.extractall(ROOT / "lessmsi")
    elif task == "fakturama":
        download(
            "https://files.fakturama.info/release/v2.2.0/Installer_Fakturama_windows-x64_2.2.0_with_jre.msi",
            ROOT / "fakturama.msi",
        )
    elif task == "git":
        data = release("git-for-windows/git")
        asset = next(
            a
            for a in data["assets"]
            if a["name"].startswith("MinGit-")
            and a["name"].endswith("-64-bit.zip")
            and "busybox" not in a["name"]
        )
        archive = download(asset["browser_download_url"], ROOT / "mingit.zip")
        with zipfile.ZipFile(archive) as package:
            package.extractall(ROOT / "git")
    elif task == "tesseract":
        download(
            "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe",
            ROOT / "tesseract-setup.exe",
        )
    elif task == "java":
        request = urllib.request.Request(
            "https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jre&os=windows&vendor=eclipse",
            headers={"User-Agent": "TJM-ImageToCash-setup"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)[0]
        package = data["binary"]["package"]
        archive = download(package["link"], ROOT / package["name"])
        if hashlib.sha256(archive.read_bytes()).hexdigest() != package["checksum"]:
            raise ValueError("Java archive checksum does not match vendor metadata")
        with zipfile.ZipFile(archive) as contents:
            contents.extractall(ROOT / "java")
        (ROOT / "java-provenance.json").write_text(
            json.dumps(
                {
                    "release": data["release_name"],
                    "url": package["link"],
                    "sha256": package["checksum"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    else:
        raise SystemExit("Choose lessmsi, fakturama, or git")
