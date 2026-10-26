# iCloud Photo Downloader → External SSD

可重复使用的 iCloud 下载脚本。通过开源第三方 [icloudpd](https://github.com/icloud-photos-downloader/icloud_photos_downloader) 下载普通照片和视频文件，按拍摄年月存放，适用于 ExFAT SSD。此项目不是 Apple 官方产品。

## Mac：先测试 10 个项目

启动脚本需要 Python 3.9+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。下载后端通过 uv 的 Python 3.10 隔离环境运行，源码固定为上游 v1.32.3 的提交 `879c561240d993d748ddb4546f935090502b16d3`，首次从 GitHub/PyPI 获取源码、依赖及必要的 Python 运行时，以后复用缓存。`icloud_backup.py` 和 `network_backend.py` 必须放在同一目录。

```bash
git clone https://github.com/zhuohanl/icloud-photo-downloader.git
cd icloud-photo-downloader
python3 icloud_backup.py --download --recent 10 --include-edits
```

也可双击 `iCloud照片备份.command`，它执行同一项 10 个项目的测试。

在本机终端输入 Apple 账户邮箱、密码、双重认证验证码；密码输入时不显示字符。脚本不读取 Safari 登录信息。认证由第三方 icloudpd 处理，登录请求发往 Apple。请勿把密码或验证码发到聊天或提交到 Git。

默认目标：

```
/Volumes/T7/icloud-photos-backup/automatic/
  2025/01/...
  2026/04/...
```

“最近 10 个”按加入 iCloud 的顺序，可能包含视频与 Live Photo，实际文件数可能超过 10。原片与可用编辑成品分别保存。XMP 默认关闭，媒体文件内原有元数据不变。现有的浏览器下载与 iPhone 导出保持原样，可能与新目录重复。

## 完整下载与重复运行

小范围测试核对通过后执行：

```bash
python3 icloud_backup.py --download --all --include-edits
```

以后重跑相同命令和目录，会检查已有匹配文件；中断后也可重跑。不要先移动、改名下载目录中的文件。它不执行文件内容哈希去重，也不保证按字节断点续传。

请求层使用连接超时 15 秒、读取超时 120 秒（上游默认 30 秒）。GET/HEAD 与已知的只读 CloudKit query/lookup POST 遇到 Timeout/ConnectionError 时，在同一个进程内等待 5、15、30 秒，最多尝试 4 次；登录 POST、修改记录等请求不自动重放。不会关闭 TLS 验证。媒体流中断保留 `.part` 文件并明确失败；恢复部分文件时要求服务端返回 HTTP 206，拒绝把完整 HTTP 200 响应追加到部分文件。

同一组安全读取请求遇到 HTTP 429 或 503 时，等待至少 60、120、240 秒后重试，总尝试次数同样不超过 4 次；如果服务器 `Retry-After` 指定更久，则按更久的时间等待，支持秒数和 HTTP 日期。如果要求等待超过一小时，停止而不是提前重试。连续 503 耗尽预算后仍会退出，保留已下载文件，避免不停请求服务。登录等 POST 返回 503 时直接停止，不自动重放认证。上游文案 `Apple iCloud is temporary refusing to serve icloudpd` 对应 503，单凭它不能判定限流或封号。此次修复前只有超时/连接失败会被重试，503 没有自动恢复。

请求层仍失败且识别到 `Cannot connect to Apple iCloud service` 后，外层默认等待 30、60、120 秒，最多重启 3 次。每次重启前重新检查 SSD 挂载和空间。源码入口正确传播退出码；脚本同时保留连接错误文本检查。其他错误直接退出，不盲目反复登录。用 `--retries 0` 关闭外层重启（请求层重试仍保留），或指定 0–10 次；第 3 次起等待上限为 120 秒。重启仍需重新扫描图库，必要时重新认证。

若反复失败，用 `--diagnose --retries 0` 执行一次。它开启上游 debug，但在本机内存中过滤掉调试正文，仅显示常规日志与提取的数字 HTTP 状态码，不保存原始调试日志。HTTP 401/403 可能涉及认证或访问限制，429 表示请求过多，5xx 表示服务端响应异常；仍需结合时间顺序判断。只有通用连接提示时不能确认根因。诊断模式会隐藏上游调试细节以及非标准提示/回溯；若进程等待输入却没显示提示，按 Ctrl+C 后使用普通模式认证。不建议直接公开上游完整 debug，因为其中可能包含 cookies、token、私人元数据或临时下载链接。

普通模式也会打印安全的 `TRANSPORT_ERROR kind=ReadTimeout host=... elapsed=...`、`HTTP_STATUS ... status=...` 或 `STREAM_ERROR`，不打印异常原文、完整 URL、cookies 或响应正文。请求未收到响应时没有 HTTP 状态码，因此旧的仅状态码诊断无法区分 DNS、TLS、连接或读取超时；上游捕获的 HTTP 调试记录还主要覆盖认证阶段。新入口从 requests 层记录异常类别。

按 iCloud 创建时间和运行电脑的本地时区归类。文件名附加资产 ID 后缀以减少重名冲突，保留 Live Photo 的照片与短视频两部分。

## 其他模式

```bash
# 不联网、不安装、不登录，仅查看将执行的命令
python3 icloud_backup.py --show-command --all --include-edits

# 登录并预演最近 10 个项目，不下载媒体；仍会保存会话信息
python3 icloud_backup.py

# 列出图库；默认只下载主图库，其他图库需分别指定
python3 icloud_backup.py --list-libraries
python3 icloud_backup.py --download --recent 10 --library '实际图库名' --destination '/Volumes/T7/icloud-photos-backup/shared'

# 自定义 SSD 路径
python3 icloud_backup.py --download --recent 10 --destination '/Volumes/MySSD/icloud-backup'
```

Windows 可运行 `.py` 并指定实际盘符，`.command` 仅用于 Mac。ExFAT 可跨平台读取，HEIC/HEVC 在某些电脑上仍需解码器。

## 数据和账户

### XMP 解压报错后的继续下载

如果旧版本出现 `xmp_sidecar.py` / `zlib.error: invalid stored block lengths`，这是上游生成 XMP 时解码调整元数据失败。更新本脚本后重跑原来的全库命令，默认不再传 `--xmp-sidecar`，避开该路径。保留已下载媒体和已有 XMP，无需删除、清空或重建目录。仍会全库检查，匹配的文件跳过。

默认关闭意味着尚未导出的 iCloud 标题、收藏等附加信息不会另存为 XMP；原媒体内已有 EXIF 等数据不受此选项影响。它不修复上游解码器；需要额外 XMP 时可显式加 `--xmp-sidecar`，但可能再次触发同样错误。媒体下载完成也不能视为完整元数据归档。

- 只下载：脚本不启用任何本地或 iCloud 删除参数，也不自动上传 AWS。
- 会话 cookies/tokens 存入本机仓库的 `.local-state/icloud-session`，仅当前用户可访问，并由 `.gitignore` 排除。不要分享或上传此目录。密码不作为命令行参数传入，也不启用 keyring 保存。
- 不同账户使用独立 `--state-directory` 和 `--destination`。
- Mac 会检查目标卷是否挂载，防止 SSD 缺席时把照片写到内置磁盘。
- 不支持的账户安全设置、Apple 接口变化、限流或网络问题可能使下载失败。项目文档说明 ADP 和硬件安全密钥认证存在不支持情况；不要为此关闭账户安全设置。
- 下载范围只有 iCloud 已上传的内容；手机未同步的照片仍须另备份。并非 Photos 图库的完整备份，不能保证相册、收藏、人物、AAE 等信息可恢复。
- 文件总数包括 Live Photo 视频、编辑成品和 XMP，不等于媒体项目数。退出成功或统计数字都不是独立的完整性证明；清理原件前须核对来源、抽查视频与编辑版本，并另存副本。

## 合并去重，准备 AWS 上传

下载完成后，可使用 `reconcile_photos.py` 将 iCloud 和 iPhone 导出按完整文件 SHA-256 合并到新的独立目录，并保留原件及来源清单。详见 [RECONCILE.md](RECONCILE.md)。

```bash
caffeinate -i python3 reconcile_photos.py --build
```

## 本地测试

```bash
python3 -m unittest discover -s tests -v
bash -n iCloud照片备份.command
```

测试不登录账户、不下载照片；首次真实下载仍需交互登录并核对结果。

`tests/test_transport.py` 需要上游源码环境，普通 Python 缺少依赖时会跳过。运行完整测试可执行：

```bash
uv run --no-project --python 3.10 --with 'icloudpd @ git+https://github.com/icloud-photos-downloader/icloud_photos_downloader.git@879c561240d993d748ddb4546f935090502b16d3' python -m unittest discover -s tests -v
```

## 参考

- [icloudpd 参数](https://icloud-photos-downloader.github.io/icloud_photos_downloader/reference.html)
- [原片与编辑成品](https://icloud-photos-downloader.github.io/icloud_photos_downloader/size.html)
- [文件命名与 Live Photo](https://icloud-photos-downloader.github.io/icloud_photos_downloader/naming.html)
- [认证及限制](https://icloud-photos-downloader.github.io/icloud_photos_downloader/authentication.html)
