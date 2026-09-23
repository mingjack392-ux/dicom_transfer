# DICOM Transfer：医学影像数据治理工具链

当前维护流程覆盖无数据库安全转存、患者身份核对、Study 报表、入组筛选、
可回溯匿名化、中心影像表、UID 二次筛选与设备信息回填。

功能入口、运行依赖和验证边界见 [项目功能基线](docs/PROJECT_BASELINE.md)。
提交范围和日常版本管理见 [Git 使用说明](docs/GIT_WORKFLOW.md)。
实际中心数据、映射表、部署指南和旧数据库脚本保留在本地，不纳入 Git。

## 目录结构

| 目录 | 内容 |
|---|---|
| `src/` | DICOM 转存、匿名化、扫描、合并等主程序及运行所需配置 |
| `tests/` | 自动化测试 |
| `scripts/linux/` | 通用文件复制和中心影像表启动脚本 |
| `scripts/benchmark_enrolled_anonymization_fast.py` | 合成数据性能对比 |
| `web_registry/` | 通用混合包转存程序；同目录的实际中心数据由 Git 排除 |
| `docs/` | 经审查的通用说明；具体中心操作指南仅本地保存 |
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

V3 转存（混入患者分流、同一自然人多 PatientID 归组）：

```powershell
python .\src\dicom_organize_v3.py "输入目录" "输出目录" --manifest
```

V3 会先完成整批 Header 预扫描，再开始复制；证据不足的身份关系写入待确认表，
不会在运行中暂停。检查明细、身份映射、待确认、来源审计和异常记录默认集中在一个
多工作表 Excel 中；跨批次状态保存在目标目录的 `.dicom_v3_state`。

单个筛选号目录只做身份门禁和UID层级转存：

```powershell
python .\src\dicom_transfer_by_screening.py "源目录\01_H001" "独立目标\示例中心"
```

该入口预检通过后增加 `--execute`，输出 `01_H001/StudyUID/SeriesUID/SOPUID.dcm`，
允许已确认属于同一人的多个 PatientID；身份冲突或证据不足时不复制。
文件夹与 ZIP/RAR 混合来源使用 `web_registry/` 下的独立入口，详见项目功能基线。

从已经匿名化完成的交付目录按 Excel 中的 Series/SOP UID 二次筛选，默认只预检：

```bash
python3 ./src/filter_anonymized_dicom_by_uid_selection.py \
  "/data/匿名化交付影像" "/data/control/匿名化映射表.xlsx" "/data/新的筛选影像" \
  --sheet "筛选序列明细" --control-dir "/data/新的筛选控制目录"
```

`SOPUID=NA` 时复制整条 Series，有具体 SOPUID 时只复制该实例；目标完整保留
`匿名编号/目标二级目录/Study/Series/SOP.dcm`，源匿名化文件不修改。核对预检审计后
再增加 `--execute`。参数说明可运行 `python src/filter_anonymized_dicom_by_uid_selection.py --help`。

运行全部测试：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

详细说明见 [项目功能基线](docs/PROJECT_BASELINE.md)、
[V2 转存](docs/DICOM_V2_README.md) 和 [V3 转存](docs/DICOM_V3_README.md)。
