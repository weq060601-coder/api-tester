"""Build a one-file portable Windows release from explicit, non-user inputs."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != "win32":
        raise SystemExit("Build on 64-bit Windows with Python 3.13.")
    python = ROOT / ".build-venv" / "Scripts" / "python.exe"
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(python.parent.parent)], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "build_tools" / "requirements.txt")], check=True)
    subprocess.run([str(python), "-m", "PyInstaller", "--noconfirm", "--distpath", str(ROOT / "dist"),
                    "--workpath", str(ROOT / "build"), str(ROOT / "build_tools" / "portable.spec")],
                   cwd=ROOT, check=True)
    executable = ROOT / "dist" / "API-Tester.exe"
    guide = (
        "本地 API 测试台 · Windows 绿色版\n\n"
        "1. 解压到可写文件夹，双击 API-Tester.exe。无需安装 Python，无需管理员权限。\n"
        "2. 支持 Windows 10/11 x64；鹈鹕/SVG 内嵌预览使用系统 Microsoft Edge WebView2 Runtime 和 .NET Framework 4.6.2+。\n"
        "   Windows 11 通常已具备；预览提示缺少组件时安装微软 WebView2 Runtime：\n"
        "   https://developer.microsoft.com/microsoft-edge/webview2/\n"
        "3. settings.json（配置和 Key）及 pelican/results（作品）自动保存在 EXE 旁。\n"
        "   更新软件时替换 EXE 即可；移动或备份时一起携带自己的数据文件。\n"
        "4. 从源码版迁移：手动把自己的 settings.json 和 pelican/results 复制到 EXE 旁即可。\n"
        "5. 单文件首次启动需要解压运行组件到系统临时目录，可能等几秒。\n"
        "6. 此发布包不含作者/用户的 Key、供应商配置、历史回复或作品；程序不会自动发起模型测试。\n"
        "7. 仅在自己电脑保存的 settings.json 含明文 Key，分享绿色版时请只分享本压缩包/原始 EXE。\n"
    )
    archive = executable.with_name("API-Tester-Windows-x64-portable.zip")
    # Never zip a working folder: the release has exactly these two entries.
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.write(executable, executable.name)
        output.writestr("使用说明.txt", guide.encode("utf-8-sig"))
    checksums = {path.name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in (executable, archive)}
    (ROOT / "dist" / "SHA256.json").write_text(json.dumps(checksums, indent=2), encoding="utf-8")
    print(json.dumps(checksums, indent=2))


if __name__ == "__main__":
    main()
