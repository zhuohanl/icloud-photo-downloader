# 合并 iPhone 导出与 iCloud 下载，生成 AWS 上传目录

`reconcile_photos.py` 使用 Python 标准库和 SHA-256，适用于 macOS/Linux；输出是普通文件，ExFAT 可在其他电脑读取，HEIC/HEVC 解码仍取决于软件支持。

默认读取：

- `/Volumes/T7/icloud-photos-backup/automatic`
- `/Volumes/T7/iphone-photos-backup`
- `/Volumes/T7/icloud-photos-backup/2026-10-24`（之前浏览器测试下载）

生成独立目录 `/Volumes/T7/reconciled-photos`。默认只计算计划；加 `--build` 才复制。所有原件保持原样，不执行 AWS 上传，也不删除重复原件。

```bash
# 只计算校验和和合并计划，结果摘要存在本机 state 目录
python3 reconcile_photos.py

# 创建唯一内容的独立副本并逐文件校验
caffeinate -i python3 reconcile_photos.py --build
```

每个源文件先做完整 SHA-256。对未改变大小和 mtime_ns 的文件，重复运行可复用本机 SQLite 校验缓存。复制时重新计算源内容哈希，再读回目标文件验证。已有目标也重新计算哈希，遇到不匹配会停止而不是覆盖。

约 190GB 输入需要较长时间。每 10 秒打印进度。输出生成期间 `STATUS.json` 为 `building`，最后清单、校验和来源快照检查完成后才设为 `complete`。中断可重跑同一命令；原文件不变，已验证存在的输出可复用。不要在运行期间下载新文件、移动或编辑两份源目录，否则最终快照核对会失败。

## 自定义来源

按 `--source` 的先后顺序决定相同内容优先使用哪个来源的文件名和目录。标签 `icloud` 用于识别现有 `YYYY/MM` 目录；`iphone` 用于统计手机独有内容。

```bash
python3 reconcile_photos.py --build \
  --source icloud=/Volumes/MySSD/cloud \
  --source iphone=/Volumes/MySSD/phone \
  --destination /Volumes/MySSD/reconciled \
  --state-directory ./local-reconcile-state
```

源、输出和 state 目录必须分开。输出若不是本脚本创建的目录，会拒绝复用。已有归档对应固定来源快照；后续新增来源文件可能改变规范文件名或引入旧输出，请使用新输出目录生成下一份快照，避免混入上次结果。

## 去重范围

- 完全相同字节内容只保存一次，即使文件名不同。
- 同名但不同内容保留；编辑版、不同压缩或元数据版本不合并。
- Live Photo 的照片与视频是不同内容，都保留；普通查看器不一定自动将其组合。
- AAE/XMP 也按内容去重后存入 `sidecars/`，不是都放在图片旁边。原始来源名及按文件名推断的媒体对应关系保存在 `manifest.jsonl` 中。移动后的 sidecar 不保证自动被照片软件应用，原导出仍保留便于恢复。
- 忽略隐藏文件（包括 macOS `._*` 和 `.DS_Store`），拒绝非隐藏的临时下载文件、零字节文件及非普通文件。目录符号链接不跟随。
- `iphone-only` 仅表示“没有在其他来源发现相同内容”，不能证明一定没有上传 iCloud：重新编码或不同元数据也会造成不同哈希。
- 不进行相似画面去重、解码质量检查，不能单独证明手机/iCloud 原始图库已全部导出。

## 输出和 AWS

```
reconciled-photos/
  media/YYYY/MM/...__完整SHA256.heic
  media/iphone-only/...
  media/unknown-date/...
  sidecars/哈希前两位/完整SHA256.aae
  manifest.jsonl
  summary.json
  REPORT.md
  README.txt
  STATUS.json
```

无需手机文件内的日期猜测：有 iCloud 年月则沿用，没有可靠来源月份的手机文件放入 `iphone-only`，其原文件内 EXIF 保留。文件名中的哈希用于避免重名和可核对，不代表新增的照片元数据。

仅当 `STATUS.json` 为 `complete` 时上传 `media/`、`sidecars/` 及清单说明文件。不要上传 `.reconcile-owned.json`、本机 state/缓存或构建中的 `_partial/`。清单包含原始照片文件名及来源路径，属于个人归档信息，应随照片放入私有存储。

清单还保留每个来源的 `filesystem_mtime_ns` 和规范副本的 `canonical_filesystem_mtime_ns`。这些是文件系统修改时间，不一定是拍摄时间；可用于从对象存储恢复文件时间，避免将 AWS 上传时间误当作照片时间。

Mac/ExFAT 会自动生成隐藏 `._*` 辅助文件，文件数量和去重清单均排除它们。上传时也应排除，避免把这些系统信息存入 S3。归档 `README.txt` 中生成了 AWS CLI `s3 sync --dryrun` 预演示例，带隐藏文件和临时文件排除规则；此脚本没有执行上传。参考 [AWS CLI sync 文档](https://docs.aws.amazon.com/cli/latest/reference/s3/sync.html)。

## 测试

```bash
python3 -m unittest discover -s tests -p test_reconcile.py -v
```

覆盖跨来源重复、同名不同内容、Live Photo 配套文件、编辑版、AAE/XMP 配对、重复运行、目标损坏拒绝覆盖、未完成/空文件以及源文件变化。
