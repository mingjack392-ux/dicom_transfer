# DICOM 项目功能基线

记录日期：2026-09-22。本文件描述当前源码及本地验证范围；生产中心的实际运行结果
以受控目录内对应批次的日志、审计和输出为准。下列路径和示例均不引用真实病例。

## 功能与入口

| 功能 | 入口（相对仓库根目录） | 关键边界 |
|---|---|---|
| 患者盘点 | `src/scan_dicom_patients.py` | 只读 Header，统计患者与 PatientID |
| V2 安全转存 | `src/dicom_organize_v2.py` | 原子复制、SHA-256 去重、UID 冲突保留、Study 汇总 |
| V3 身份归组 | `src/dicom_organize_v3.py` | 混入患者分流、多 PatientID 人口学证据核对、跨批次状态 |
| V3 纯 Python 报表入口 | `src/dicom_organize_v3_linux.py` | 复用 V3；`--report-only` 仅从已有状态补报表 |
| 筛选号目录转存 | `src/dicom_transfer_by_screening.py` | 单目录或 `--batch`；身份预检通过后才复制 |
| 筛选号混合包转存 | `web_registry/dicom_transfer_screening_packages_windows.py`、`web_registry/dicom_transfer_screening_packages_linux.py` | 文件夹/ZIP/RAR 聚合、补充包、扫描检查点；RAR 需 7z/7zz |
| 中心影像表 | `src/analyze_transferred_dicom_by_center.py`、`src/analyze_transferred_dicom_by_center_linux.py` | 单帧按 Series、多帧按 SOP；限定中心匹配登记表；支持 H/Q 筛选号 |
| 入组 Study 白名单 | `src/build_enrolled_study_selection.py`、`src/build_enrolled_study_selection_linux.py` | 入组表、序列表及登记表交叉匹配，生成待审核清单与稳定编号账本 |
| 入组 Study 匿名化 | `src/anonymize_enrolled_studies.py`、`src/anonymize_enrolled_studies_linux.py` | 只处理审核清单中的完整 Study |
| 高速匿名化 | `src/anonymize_enrolled_studies_fast_windows.py`、`src/anonymize_enrolled_studies_fast_linux.py` | 文件批次并发、回读验证、断点续跑 |
| 无文件头兼容匿名化 | `src/anonymize_enrolled_studies_fast_headerless_windows.py`、`src/anonymize_enrolled_studies_fast_headerless_linux.py` | 独立兼容入口；满足隐式 VR 小端、路径和 UID 门禁才处理 |
| 前瞻性入组及匿名化 | `src/build_enrolled_study_selection_prospective_linux.py`、`src/anonymize_enrolled_studies_fast_prospective_linux.py` | 只接受 Q 筛选号，独立编号账本和交付/控制目录 |
| 转存后按清单筛选 | `src/filter_transferred_dicom_by_selection.py` | 术前选择整 Series，术中选择具体 SOP；以实际源患者目录为范围 |
| 匿名后 UID 二次筛选 | `src/filter_anonymized_dicom_by_uid_selection.py` | 整 Series 或精确 SOP，保持相对目录和文件字节 |
| 厂家、型号回填 | `src/fill_dicom_equipment.py` | 只读 DICOM Header，将两列设备信息写入新的 Excel，生成审计 |
| 通用转存后匿名化 | `src/dicom_anonymize_transferred.py` | 稳定 E 编号、复合身份匹配、敏感映射和审计 |
| 原始包匿名化分类 | `src/dicom_anonymize_classify.py` | 文件夹/ZIP 整包分类、UID 重映射，属于另一套业务流程 |

`src/merge_patients.py`、`src/migrate_existing_output_names.py` 是历史目录整理工具，
需按具体目录单独审查。旧 MySQL 入口及旧 Windows 启动器仅在本地保留，不属于本基线。

## 依赖与首次检查

Python 建议使用 3.10 或更高版本。在自己管理的虚拟环境中执行：

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -p "test_*.py" -v
```

`requirements-test.txt` 包括 pydicom、openpyxl 和用于旧 `.xls` 表的 xlrd。
新建 Excel 和 Linux 报表流程使用 openpyxl，不需要数据库。

Windows 原 V2/V3 报表和中心表入口的 `.mjs` 辅助模块依赖 Node.js 与
`@oai/artifact-tool`。该包由既有运行环境提供，本仓库未将其依赖目录入库；普通环境
可使用复用同一业务规则、通过 openpyxl 读写的 `*_linux.py` 入口。部署前运行相应
入口的 `--help` 并核对依赖。Python 回归测试不代表每个 Node 报表入口都已部署验收。

各项命令的准确参数以当前源码 `--help` 为准：

```bash
python src/build_enrolled_study_selection.py --help
python src/anonymize_enrolled_studies_fast_linux.py --help
python src/fill_dicom_equipment.py --help
python web_registry/dicom_transfer_screening_packages_linux.py --help
```

## 使用示例

V3 正式转存会直接复制到独立目标（不是预览命令）：

```bash
python src/dicom_organize_v3_linux.py "/data/input" "/data/organized" --workers 1 --copy-workers 2
```

筛选号混合包默认只预检，核对审计后才增加 `--execute`：

```bash
python web_registry/dicom_transfer_screening_packages_linux.py batch "/data/packages" "/data/organized-center"
```

入组匿名化默认预览；清单须先经人工审核：

```bash
python src/anonymize_enrolled_studies_fast_linux.py \
  --source-center "/data/organized-center" \
  --study-list "/data/control/reviewed-studies.xlsx" \
  --output-root "/data/delivery/01.交付影像" \
  --control-dir "/data/delivery/02.内部控制文件" \
  --center-alias "示例中心" --workers 4 --dicom-workers 4 --backend thread
```

设备信息回填默认生成预检审计，增加 `--execute` 才写新的 Excel：

```bash
python src/fill_dicom_equipment.py "/data/selected" "/data/control/selection.xlsx" "/data/control/selection-with-equipment.xlsx" --control-dir "/data/equipment-audit"
```

## 规则与人工复核

- V3 的 `needs_review` 表示证据不足，不能解释为已经确认是不同患者。
  `grouped_needs_review` 只是待复核的目录归组。
- `3D_DSA断层` 是本项目的元数据规则：XA、层厚有值、描述命中重建关键词须同时满足。
- 手术时期来自外部登记表与影像日期的匹配，不能单凭 DICOM 猜测手术时间。
- 前瞻性所有中心共用一套独立账本，按中心编号和 Q 筛选号复用匿名编号；不得与回顾性混用。
- 入组匿名化保留 PatientID、非出生日期的日期时间、所有 UID 及 PixelData，并移除规定的
  姓名、出生日期、机构及中心文本。这是客户规则下的可回溯伪匿名化，不处理烧录像素文字。
- 高速版默认严格 UID 策略。`--uid-policy preserve` 是明确保留非标准 UID 的兼容策略，
  仍逐项验证 UID；缺失 UID、路径不安全、Study 不符或不可读继续阻断。
- `--confirmed-multi-patient-study STUDY_UID` 只用于已有人工确认的精确 Study，确认事实
  进入审计与断点；未列出的多 PatientID Study 仍阻断。
- 无文件头兼容只适用于门禁通过的文件；不能将无文件头许可与 preserve 策略组合使用。
- 断点、映射、审计文件属于内部控制数据；`--report-only` 无法重扫影像或应用新分类规则。

## 版本与验证

当前源码版本：V2 分类 `2026.08.21-study-v2.1`，V3 身份 `2026.08.21-identity-v3.4`，
中心表 `2026.09.21-center-web-v1.3`，入组筛选 `2026.09.14-enrolled-selection-v1.5`，
入组匿名化规则 `2026.09.07-enrolled-anon-v1.4`，高速引擎 `2026.09.21-enrolled-anon-fast-v1.2.1`。

2026-09-22 在 Windows / Python 3.11.15、pydicom 3.0.2、openpyxl 3.1.5、xlrd 2.0.2
环境执行现有 16 个测试模块：181 项中 180 项通过、1 项跳过、无失败。
跳过的是设备回填的符号链接越界用例，当前 Windows 进程无创建符号链接权限。
测试均使用合成数据；部分测试模拟报表写入，因此结果不代表所有外部依赖或生产服务器验收。

首次沙箱运行遇到临时目录访问限制；在获准的本地测试环境重跑后得到上述结果。
真实数据、中心操作记录和发布 ZIP 未纳入本次版本，均按原位置在受控本地存储保留。
