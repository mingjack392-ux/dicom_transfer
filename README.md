# DICOM Transfer 项目导航

项目已按功能归类。根目录只保留功能目录、数据目录和输出目录。

## 目录结构

| 目录 | 内容 |
|---|---|
| `src/` | DICOM 转存、匿名化、扫描、合并等主程序及运行所需配置 |
| `tests/` | 自动化测试 |
| `scripts/windows/` | Windows 批处理、备份和监控脚本 |
| `scripts/linux/` | Linux 文件复制与受控删除脚本 |
| `scripts/sql/` | 数据库建表和分类 SQL |
| `docs/` | 项目说明、V2 说明和设计报告 |
| `data/reference/` | 名单、迁移计划和参考表格 |
| `logs/` | 历史运行日志、状态和实时进度文件 |
| `bin/` | 已打包的 Windows 可执行程序 |
| `archive/patch_helpers/` | 历史一次性修补脚本，不作为当前入口 |
| `dicom/` | 本地 DICOM 数据目录（保持原位） |
| `_bench_out/` | 基准测试输出（保持原位） |
| `outputs/` | 报表、规则表和其他生成结果 |

根目录的 `dicom_anonymize_transferred.py` 是兼容启动器，用于继续支持整理前的旧命令；实际实现位于 `src/`。

## 常用入口

转存后匿名化，默认仅预演：

```powershell
python .\src\dicom_anonymize_transferred.py "输入目录" "输出目录" --single-patient
```

确认后执行：

```powershell
python .\src\dicom_anonymize_transferred.py "输入目录" "输出目录" --single-patient --execute
```

批量处理时输入包含多个患者文件夹的根目录，并省略 `--single-patient`。患者身份按姓名、PatientID、出生日期、性别、年龄与检查日期综合判断；证据充分的多 PatientID 数据会归入同一个 `E######`，证据不足时分开编号并在映射表标记 `needs_review`。

敏感映射表包含 `PatientID`、出生日期、性别、年龄、同一 E 编号下的全部 PatientID、匹配状态和匹配依据。旧版四列映射表可继续读取，并在下次正式执行时升级为新格式。

V2 转存：

```powershell
python .\src\dicom_organize_v2.py "输入目录" "输出目录"
```

运行全部测试：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

详细说明见 [`docs/项目文档.md`](docs/项目文档.md) 和 [`docs/DICOM_V2_README.md`](docs/DICOM_V2_README.md)。
