"""Build a folder portable ZIP in a fresh staging directory, without user data."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != "win32":
        raise SystemExit("Build on 64-bit Windows with Python 3.13.")
    python = ROOT / ".build-venv" / "Scripts" / "python.exe"
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(python.parent.parent)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "build_tools" / "requirements.txt")], check=True)
    guide = (
        "本地 API 测试台 · Windows 文件夹绿色版\n\n"
        "1. 完整解压压缩包，打开 API-Tester 文件夹，双击 启动.bat 或 API-Tester.exe。\n"
        "   无需安装 Python，无需管理员权限；不要在压缩包内直接运行，不要单独移动 EXE。\n"
        "   _internal 文件夹包含运行依赖，必须与 EXE 一起保留；此版启动无需临时解包组件。\n"
        "2. 支持 Windows 10/11 x64；鹈鹕/SVG 内嵌预览使用系统 Microsoft Edge WebView2 Runtime 和 .NET Framework 4.6.2+。\n"
        "   Windows 11 通常已具备；预览提示缺少组件时安装微软 WebView2 Runtime：\n"
        "   https://developer.microsoft.com/microsoft-edge/webview2/\n"
        "3. settings.json（配置和 Key）及 pelican/results（作品）自动保存在 EXE 旁。\n"
        "   更新时解压到新文件夹，再把自己的配置和作品复制过去；移动或备份时携带整个文件夹。\n"
        "4. 从源码版迁移：手动把自己的 settings.json 和 pelican/results 复制到 EXE 旁即可。\n"
        "5. 不需要原源码文件夹、虚拟环境或全局 Python。关闭程序后可直接移动整个文件夹。\n"
        "6. 此发布包不含作者/用户的 Key、供应商配置、历史回复或作品；程序不会自动发起模型测试。\n"
        "7. 仅在自己电脑保存的 settings.json 含明文 Key，分享绿色版时请分享原始发布压缩包。\n"
    )
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    work = ROOT / "build"
    work.mkdir(exist_ok=True)
    archive = destination / "API-Tester-Windows-x64-folder-portable.zip"
    # Build and enumerate only a freshly owned directory, never an existing
    # release folder where someone may already have saved Keys or artworks.
    with tempfile.TemporaryDirectory(prefix="folder-release-", dir=work) as staging:
        subprocess.run([str(python), "-m", "PyInstaller", "--noconfirm", "--distpath", staging,
                        "--workpath", str(work), str(ROOT / "build_tools" / "portable.spec")],
                       cwd=ROOT, check=True)
        release = Path(staging) / "API-Tester"
        executable = release / "API-Tester.exe"
        assert executable.is_file() and (release / "_internal" / "python313.dll").is_file()
        (release / "使用说明.txt").write_bytes(guide.encode("utf-8-sig"))
        (release / "启动.bat").write_bytes(
            b'@echo off\r\ncd /d "%~dp0"\r\nstart "" "%~dp0API-Tester.exe"\r\n')
        paths = sorted(path for path in release.rglob("*") if path.is_file())
        manifest = {}
        for path in paths:
            relative = path.relative_to(release).as_posix()
            if path.name.lower() in ("settings.json", "settings.json.tmp") or any(
                part in relative.lower() for part in ("pelican/results/", "pelican/examples/", "pelican/diagnostics/")):
                raise RuntimeError(f"Unexpected user data in fresh release: {relative}")
            manifest[relative] = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in paths:
                output.write(path, "API-Tester/" + path.relative_to(release).as_posix())
            output.writestr("API-Tester/文件校验.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    checksums = {archive.name: {"bytes": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}}
    (destination / "folder-portable-SHA256.json").write_text(json.dumps(checksums, indent=2), encoding="utf-8")
    print(json.dumps(checksums, indent=2))


if __name__ == "__main__":
    main()
