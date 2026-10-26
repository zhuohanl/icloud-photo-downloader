# AWS 上传规划与命名

本目录包含基于清单的上传和验证工具。不会操作 T7 上其他备份目录或其他 buckets。

本批使用 **`ap-southeast-2`、S3 Intelligent-Tiering（`INTELLIGENT_TIERING`）**，bucket 为 `personal-archive-ap-southeast-2-69a8433259`。`config.example.json` 是通用模板，bucket 和 expected_account 需填入本机配置；不含凭据。

Intelligent-Tiering 的可选归档层及相应规则尚未决定，本配置没有启用任何归档规则。

## 上传与断点继续

以下命令从仓库根目录执行，使用已有 AWS CLI 默认凭据链，不修改全局配置。安装依赖到本机虚拟环境：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r aws/requirements.txt
```

复制模板为 `aws/config.local.json`，填入目标 bucket 和 expected_account（12 位账户 ID）。该文件和 `.local-state/` 均被 Git 忽略。不要把密钥写入配置。

```bash
# 只核对本地清单、大小及目录布局，不调用 AWS
.venv/bin/python aws/upload_archive.py --config aws/config.local.json

# 首次创建新 bucket 才额外使用 --create-bucket；续跑绝不加此参数
# 小样本包含普通文件和一个最小的分段上传文件，不发布完成标记
caffeinate -i .venv/bin/python -u aws/upload_archive.py \
  --config aws/config.local.json --execute --limit 20

# 全量上传，或中断后使用完全相同的命令继续
caffeinate -i .venv/bin/python -u aws/upload_archive.py \
  --config aws/config.local.json --execute --retry-transient --workers 3 --bandwidth-mib 20
```

共享传输并发默认 3、带宽上限默认 20 MiB/s；与其他上传共享网络时可降低。上传进度在 `.local-state/aws/progress.json`，逐文件验证日志在 `verified.jsonl`，仅保存在本机。文件级续跑会读取本地内容，并重新验证 S3 已有对象；中断的单个大文件可能重新上传，已经完成的文件不重复上传。`--retry-transient` 对连接或服务暂时故障自动退避后续跑；权限、内容校验和本地磁盘错误会停止，不无限重试。

安全与校验规则：

- 要求归档 STATUS 为 complete、layout 为 calendar，清单条目数和总字节数相符；逐文件重新计算完整 SHA-256 并比对清单。
- 新 bucket 开启 Block Public Access、禁用 ACL、使用 SSE-S3；上传和读取使用 HTTPS，并检查预期 AWS 账户及 bucket owner。全程不执行删除、不改动其他 buckets，不自动覆盖同 key 下内容不一致的对象。
- 显式请求 S3 SHA-256 校验，上传后读取 S3 保存的校验值；普通文件比较完整 SHA-256，分段文件比较按固定 64 MiB 分段计算的复合 SHA-256，同时检查大小、存储类别和服务端加密。完整文件 SHA-256 另外记录在清单和对象 metadata；不把 multipart ETag 当作完整文件哈希。[AWS 校验说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html)。
- 只有全部 payload 验证通过才发布快照清单及 `cloud-layout.json`，最后发布 `UPLOAD-VERIFIED.json`。恢复时将清单 target 接在 library_prefix 后，并比对完整文件 SHA-256；源 STATUS 只代表本地归档，不能代替云端完成标记。
- 上传、列举、逐文件 HEAD 校验会产生 S3 请求费用，另有 Intelligent-Tiering 适用的监控和存储费用。恢复下载也应考虑传输费用。[S3 价格](https://aws.amazon.com/s3/pricing/)。

建议后续为需要的前缀使用最小权限短期身份，并按审计需要评估访问日志、CloudTrail 数据事件和 CloudWatch 请求指标；这些额外日志和监控可能计费，本工具不自动开启。日志目标应加密。

```bash
.venv/bin/python -m unittest discover -s aws/tests -v
```

## 本地归档

`/Volumes/T7/photo-backup-iphone-icloud-2026-10-24`

名称保留本批次的来源与日期。日期表示来源导出批次，不表示所有照片都拍于该日，也不表示整理完成日期。历史照片仍按各自年月保存在 media/ 中。

## Bucket：长期稳定的名称

模式：`personal-archive-<region>-<unique-suffix>`

按暂定 Region，候选名称模式为：

`personal-archive-ap-southeast-2-<unique-suffix>`

正式创建时再选定唯一性后缀并检查占用情况。名称不绑定设备、iCloud、日期、某个存储类别或目录格式版本，因此将来增加相机、Android、其他云来源或文档无需重命名 bucket。名称不要含邮箱、个人身份信息或密码。

普通全局命名空间的通用 bucket 名需在 AWS 分区内唯一；创建后不能改名。建议只用小写字母、数字和连字符，避免点号的额外兼容问题。[命名规则](https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html)、[创建后名称与区域限制](https://docs.aws.amazon.com/AmazonS3/latest/userguide/create-bucket-overview.html)。

## 前缀：媒体库与备份清单分开

```text
s3://<bucket>/
  photos/
    library/
      v1/
        media/2010/06/...
        media/2026/10/...
        sidecars/...
    snapshots/
      2026-10-24/
        manifest.jsonl
        summary.json
        calendar-moves.jsonl
        REPORT.md
        README.txt
        STATUS.json
        cloud-layout.json
        UPLOAD-VERIFIED.json
      <future-batch-date>/
        ...下一批清单和报告...
  documents/
    ...未来的其他个人归档...
```

S3 的“目录”是 object key 前缀；以上是逻辑布局，不需要预建空目录。[AWS 前缀说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-prefixes.html)。

本地映射：

| 本地内容 | 推荐 S3 前缀 |
|---|---|
| media/ | photos/library/v1/media/ |
| sidecars/ | photos/library/v1/sidecars/ |
| 清单、报告、日期移动记录 | photos/snapshots/2026-10-24/ |

manifest 内的 target 是相对于 `photos/library/v1/` 的路径，例如 `media/2026/10/...heic`。正式上传实现应给每批清单附带 library_prefix=photos/library/v1/ 与 layout_version=1，保证恢复程序能将清单与媒体库对应。

## 向前与向后兼容

- 旧照片仍可放 media/2010/06/ 等年月前缀；历史备份另加 snapshots/批次日期，不改变 bucket。
- 新设备或来源记录在 manifest.sources，不再写死在 bucket 名中。
- 新批次继续复用同一个 library/v1 媒体库，只新增或更新相应对象，并保留新的清单快照。
- 将来目录/清单格式不兼容时，可明确引入 v2 并保留旧版恢复规则；v1 是本方案的布局版本，不是 S3 Versioning。
- 日期或文件名修正可能改变对象 key，即使 SHA-256 相同也会形成第二个对象。上传实现应按清单哈希识别并处理这类变化；命名方案本身不提供跨 key 自动去重。

不要每次把整批媒体放进新的日期前缀；同样内容在不同 key 下仍会占用存储，另有请求与可能的检索/传输费用。固定媒体前缀加小型清单快照更适合持续增量归档。[S3 计费项目](https://aws.amazon.com/s3/pricing/)。

## 实施时保留的约束

- 保持 bucket 私有、Block Public Access 开启；使用 HTTPS、默认服务端加密和仅能访问所需前缀的短期上传身份。[安全实践](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html)。
- 如启用访问日志、CloudTrail 数据事件或额外 CloudWatch 指标，需同时考虑日志存储、请求及监控费用；日志目标也需加密。此处没有开启这些配置。
- 上传只包含媒体、sidecars 和指定清单；排除 `._*`、其他隐藏辅助文件、构建临时文件和本机登录/哈希缓存。
- T7 上的 README.txt 只保留独立恢复归档所需的简短说明；通用命令与规划留在 GitHub。上传按上表分开映射媒体和快照，实际完成状态以云端 UPLOAD-VERIFIED.json 为准。
