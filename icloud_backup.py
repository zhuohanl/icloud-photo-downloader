#!/usr/bin/env python3
"""Reusable, download-only iCloud backup wrapper. Python 3.9+; see README."""
import argparse
import codecs
from collections import Counter
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

VERSION = "1.32.3"
DEFAULT_DEST = Path("/Volumes/T7/icloud-photos-backup/automatic")
DEFAULT_STATE = Path(__file__).resolve().parent / ".local-state" / "icloud-session"


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="iCloud 照片下载到 SSD；默认预演最近 10 个项目。")
    parser.add_argument("--username", help="Apple 账户邮箱；省略时交互输入")
    parser.add_argument("--destination", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--state-directory", type=Path, default=DEFAULT_STATE,
                        help="本机登录会话目录；不要上传或放在共享 SSD")
    parser.add_argument("--library", help="省略时使用工具默认主图库；共享图库请先 --list-libraries")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--recent", type=positive, help="只检查最近加入 iCloud 的 N 个项目")
    scope.add_argument("--all", action="store_true", help="检查所选图库全部项目")
    parser.add_argument("--download", action="store_true", help="实际下载；不加此参数仅联网预演")
    parser.add_argument("--include-edits", action="store_true", help="原片之外也下载可用的编辑成品")
    parser.add_argument("--xmp-sidecar", action="store_true",
                        help="额外导出 XMP；上游部分元数据解压会失败，默认关闭")
    parser.add_argument("--list-libraries", action="store_true", help="登录后列出可用图库")
    parser.add_argument("--retries", type=int, choices=range(0, 11), default=3,
                        metavar="0..10", help="已识别的连接失败最多重试次数，默认 3")
    parser.add_argument("--show-command", action="store_true", help="仅显示命令，不安装、不登录、不下载")
    return parser.parse_args(argv)


def download_arguments(args, destination, state):
    command = ["--username", args.username, "--directory", str(destination),
               "--cookie-directory", str(state), "--password-provider", "console",
               "--mfa-provider", "console", "--log-level", "info"]
    if args.library:
        command += ["--library", args.library]
    if args.list_libraries:
        return command + ["--list-libraries"]
    command += ["--folder-structure", "{:%Y/%m}", "--size", "original",
                "--live-photo-size", "original", "--live-photo-mov-filename-policy", "original",
                "--file-match-policy", "name-id7"]
    if args.xmp_sidecar:
        command += ["--xmp-sidecar"]
    if args.include_edits:
        command += ["--size", "adjusted"]
    if not args.all:
        command += ["--recent", str(args.recent or 10)]
    if not args.download:
        command += ["--dry-run"]
    return command


def runner():
    # Prefer an isolated pinned PyPI package, rather than an unknown system version.
    uv = shutil.which("uv")
    if not uv:
        candidate = Path.home() / ".local" / "bin" / "uv"
        if candidate.is_file():
            uv = str(candidate)
    if uv:
        return [uv, "tool", "run", "--from", "icloudpd==" + VERSION, "icloudpd"]
    executable = shutil.which("icloudpd")
    if executable:
        version = subprocess.run([executable, "--version"], capture_output=True, text=True)
        if version.returncode == 0 and VERSION in version.stdout.split():
            return [executable]
    raise RuntimeError("需要 uv，或安装 icloudpd==" + VERSION + "。见 README。")


def validate_destination(destination):
    # Never silently create /Volumes/T7 on the Mac's internal drive if SSD is absent.
    if sys.platform == "darwin":
        if len(destination.parts) < 4 or destination.parts[1] != "Volumes":
            raise RuntimeError("Mac 下载位置必须位于 /Volumes/<外置磁盘>/<文件夹>。")
        volume = Path(*destination.parts[:3])
        if not volume.is_mount():
            raise RuntimeError("外置磁盘未挂载：" + str(volume))
        if volume.is_symlink():
            raise RuntimeError("磁盘路径不能是符号链接。")
    ancestor = destination
    while not ancestor.exists():
        ancestor = ancestor.parent
    if shutil.disk_usage(ancestor).free < 2 * 1024 ** 3:
        raise RuntimeError("目标磁盘剩余不足 2GiB，请先腾出空间。")


def summarize(destination):
    counts = Counter()
    total = 0
    errors = []
    def onerror(error):
        errors.append(str(error))
    for root, dirs, files in os.walk(destination, onerror=onerror, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            path = Path(root) / name
            if path.is_symlink():
                continue
            try:
                total += path.stat().st_size
                counts[path.suffix.lower() or "[无扩展名]"] += 1
            except OSError as error:
                errors.append(str(error))
    print("\n当前自动下载目录：", destination)
    print("文件数（含 XMP 等辅助文件）：", sum(counts.values()))
    print("文件总大小：{:.2f} GB".format(total / 1_000_000_000))
    print("文件类型：", dict(sorted(counts.items())))
    if errors:
        print("有 {} 项读取错误，统计不完整。".format(len(errors)))
    print("文件数不等于照片项目数；此统计不能证明完整备份。")


def run_once(command):
    """Relay prompts immediately; recognize Apple's connection error even on exit 0."""
    marker = "Cannot connect to Apple iCloud service"
    connection_failed = False
    tail = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    try:
        while True:
            chunk = os.read(child.stdout.fileno(), 4096)
            if not chunk:
                break
            message = decoder.decode(chunk)
            sys.stdout.write(message)
            sys.stdout.flush()
            tail = (tail + message)[-8192:]
            connection_failed = connection_failed or marker in tail
        sys.stdout.write(decoder.decode(b"", final=True))
        return child.wait(), connection_failed
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    finally:
        child.stdout.close()


def run_with_retries(command, destination, retries):
    for attempt in range(retries + 1):
        validate_destination(destination)
        code, connection_failed = run_once(command)
        if not connection_failed:
            return code
        if attempt == retries:
            print("\n连接失败，已用完自动重试次数；保留全部已下载文件。", file=sys.stderr)
            return code or 75
        delay = min(30 * 2 ** attempt, 120)
        print("\n连接中断，{} 秒后重试（{}/{}）；重新扫描并跳过匹配文件。".format(
            delay, attempt + 1, retries), flush=True)
        time.sleep(delay)
    return 75


def main(argv=None):
    args = arguments(argv)
    if not args.username:
        args.username = "YOUR_APPLE_ACCOUNT" if args.show_command else input("Apple 账户邮箱：").strip()
    if not args.username or args.username.startswith("-"):
        raise RuntimeError("请提供有效账户邮箱。")
    destination = args.destination.expanduser().absolute()
    state = args.state_directory.expanduser().absolute()
    options = download_arguments(args, destination, state)
    if args.show_command:
        print(shlex.join(["uv", "tool", "run", "--from", "icloudpd==" + VERSION, "icloudpd"] + options))
        return 0
    validate_destination(destination)
    if sys.platform == "darwin" and (state == Path("/Volumes") or Path("/Volumes") in state.parents):
        raise RuntimeError("登录会话应放在 Mac 本地，不应放在外置磁盘。")
    command = runner()
    # Preflight the installed version's options before prompting for any credentials.
    help_result = subprocess.run(command + ["--help"], capture_output=True, text=True)
    if help_result.returncode:
        raise RuntimeError("下载工具启动失败，请在终端检查 uv/PyPI 网络连接。")
    required = {value for value in options if value.startswith("--")}
    missing = sorted(flag for flag in required if flag not in help_result.stdout)
    if missing:
        raise RuntimeError("工具不支持参数：" + ", ".join(missing))
    os.umask(0o077)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    if args.download and not args.list_libraries:
        destination.mkdir(parents=True, exist_ok=True)
    print("工具：icloudpd " + VERSION + "（开源第三方，非 Apple 官方）")
    print("目标：", destination)
    print("范围：", "全部所选图库" if args.all else "最近加入的 {} 个项目".format(args.recent or 10))
    print("模式：", "列出图库" if args.list_libraries else "实际下载" if args.download else "联网预演，不下载媒体")
    if not args.list_libraries:
        print("额外 XMP：", "开启（可能遇到上游元数据解压错误）" if args.xmp_sidecar else "关闭；保留媒体文件内原有元数据")
    print("请在终端输入密码及验证码。会话保存在本机：", state, flush=True)
    code = run_with_retries(command + options, destination, args.retries)
    if code:
        print("本次未成功结束。保留已下载文件，排除错误后重跑同一命令。", file=sys.stderr)
    if args.download and not args.list_libraries and destination.exists():
        summarize(destination)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as error:
        print("错误：" + str(error), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\n已停止；保留已下载文件。", file=sys.stderr)
        sys.exit(130)
