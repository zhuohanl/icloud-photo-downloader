# AWS 上传规划与命名

本方案仅设计名称和布局，没有创建 bucket、验证名称可用性或上传照片。

**当前待决定：Region、最终 bucket 名、存储类别。没有选定悉尼或任何其他 region。** `config.example.json` 中以 null 标记；配置模板不含凭据，不会自动执行上传。AWS 上传和验证程序之后放在本目录，目前尚未实现。

## 本地归档

`/Volumes/T7/photo-backup-iphone-icloud-2026-10-24`

名称保留本批次的来源与日期。日期表示来源导出批次，不表示所有照片都拍于该日，也不表示整理完成日期。历史照片仍按各自年月保存在 media/ 中。

## Bucket：长期稳定的名称

模式：`personal-archive-<region>-<unique-suffix>`

Region 决定后再替换下面的占位符：

`personal-archive-<region>-<unique-suffix>`

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
- T7 上的 README.txt 只保留独立恢复归档所需的简短说明；通用命令与规划留在 GitHub。本方案的正式上传应按上表分开映射媒体和快照。当前没有运行任何 AWS 上传命令。
