# iCloud Photo Downloader → External SSD

可重复使用的 iCloud 下载脚本。通过开源第三方 [icloudpd](https://github.com/icloud-photos-downloader/icloud_photos_downloader) 下载普通照片和视频文件，按拍摄年月存放，适用于 ExFAT SSD。此项目不是 Apple 官方产品。

## Mac：先测试 10 个项目

需要 Python 3.9+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，或已安装 `icloudpd==1.32.3`。首次运行时 uv 从 PyPI 获取固定版本及其依赖。

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

## 本地测试

```bash
python3 -m unittest discover -s tests -v
bash -n iCloud照片备份.command
```

测试不登录账户、不下载照片；首次真实下载仍需交互登录并核对结果。

## 参考

- [icloudpd 参数](https://icloud-photos-downloader.github.io/icloud_photos_downloader/reference.html)
- [原片与编辑成品](https://icloud-photos-downloader.github.io/icloud_photos_downloader/size.html)
- [文件命名与 Live Photo](https://icloud-photos-downloader.github.io/icloud_photos_downloader/naming.html)
- [认证及限制](https://icloud-photos-downloader.github.io/icloud_photos_downloader/authentication.html)
