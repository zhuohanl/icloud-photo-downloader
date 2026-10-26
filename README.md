# Photo backup workflow

照片备份工具分为三个模块。通用代码和文档进 GitHub，实际照片和恢复清单留在 SSD；登录会话、缓存和本机配置只留本机，不提交。

```text
icloud/       iCloud 下载、网络重试和对应测试
reconcile/    精确去重、日期整理和对应测试
aws/          S3 上传、续传、校验、bucket/前缀规划与测试
.local-state/ 本机运行状态和缓存，Git 忽略
```

以下命令从仓库根目录执行。

## 1. 下载 iCloud

```bash
python3 icloud/icloud_backup.py --download --recent 10 --include-edits
# 小范围测试后，再使用 --all
```

也可双击 `icloud/iCloud照片备份.command`。需要 uv；密码和验证码只在本机终端输入。详见 [icloud/README.md](icloud/README.md)。

## 2. 去重与日期整理

```bash
caffeinate -i python3 reconcile/reconcile_photos.py --build
python3 reconcile/organize_calendar.py --exiftool /path/to/exiftool --apply
```

ExifTool 只读取日期，不修改媒体内容。具体来源、缓存、重复运行和恢复规则见 [reconcile/README.md](reconcile/README.md)。不要对正在下载或正在整理的归档并行运行其他写入任务。

## 3. AWS 规划

Region 使用 `ap-southeast-2`，存储类别使用 S3 Intelligent-Tiering（`INTELLIGENT_TIERING`）。本批 bucket 为 `personal-archive-ap-southeast-2-69a8433259`。上传是否完整，以对应快照里的 `UPLOAD-VERIFIED.json` 为准。

[aws/README.md](aws/README.md) 记录上传命令、校验规则与媒体库/快照布局；`aws/config.example.json` 是不含凭据的模板。本机配置 `aws/config.local.json` 已被 Git 忽略，需填入目标 bucket 和预期 AWS 账户。默认只检查本地计划，传入 `--execute` 才会访问 AWS 并上传。

当前本机控制器按墨尔本时间运行：工作日 20:00–次日 07:00 上传，07:00–20:00 暂停；周末全天。通过 `aws/schedule_upload.py` 启动或接管上传器；电脑重启后需重新启动控制器。

## 文件放在哪里

| 位置 | 内容 |
|---|---|
| GitHub | 本仓库脚本、测试、通用文档、配置模板 |
| T7 / 未来 S3 | media/、sidecars/、manifest.jsonl、summary.json、calendar-moves.jsonl、STATUS.json；本批 REPORT.md 与简短恢复 README.txt |
| 仅本机 | iCloud cookies/tokens、AWS 凭据、`.local-state/` 中哈希缓存和日期整理日志、本机配置 |

实际照片清单含文件名、日期等私人信息，不提交到 GitHub。Mac 隐藏辅助文件和临时构建文件也不上传 S3。原始 iPhone/iCloud 两份导出继续保留，待 S3 上传验证完成后再决定保留策略。

## 测试

```bash
python3 -m unittest discover -s icloud/tests -v
python3 -m unittest discover -s reconcile/tests -v
bash -n icloud/iCloud照片备份.command
# 安装 aws/requirements.txt 后
python3 -m unittest discover -s aws/tests -v
```

传输后端测试需要 icloud/README.md 中说明的上游源码依赖；缺少依赖时会跳过，不登录真实账户。
