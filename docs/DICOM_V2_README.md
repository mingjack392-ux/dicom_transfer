# DICOM 转存与 Study 级影像检查明细 V2

新版入口为 `dicom_organize_v2.py`。它与旧版 `dicom_organize.py` 完全独立，
不连接 MySQL，不读取或修改任何数据库。

## 当前阶段的输出

程序在完成原始 DICOM 转存后生成：

- `影像检查明细.xlsx`：每个 StudyInstanceUID 一行；
- `待确认记录.csv`：缺少 UID、日期异常、患者冲突和转存冲突；
- `转存清单.csv`：可选的文件级清单，只有使用 `--manifest` 时生成。

由于目前没有患者手术时间，程序不会猜测术中、术后 6 个月或术后 12 个月。
取得“住院号、姓名、手术时间”名单后，再用检查明细计算黄色六列。

## 运行

```powershell
python .\src\dicom_organize_v2.py "D:\原始数据" "D:\DICOM输出"
```

如果源目录下直接放着几百位患者目录，建议一次性处理整个根目录。脚本会优先从
`昌永杰_1033475_1` 这类目录名提取中文姓名，并生成 `昌永杰_1033475` 目标目录；
DICOM 内的拼音 `PatientName` 只作为没有中文目录名时的回退。

如果设备偶发把 `PatientID` 写成 `1010379!完整UUID`，程序只移除该 UUID 后缀，
归入正常的 `1010379`；其他格式的 PatientID 不会被截断。

性能参数：`--workers` 控制标签解析线程（默认 8），`--copy-workers` 控制复制线程
（默认 2）。只有确实需要每个文件立即强制落盘时才使用 `--durable`，该选项会明显变慢。

指定输出位置：

```powershell
python .\src\dicom_organize_v2.py `
  "D:\原始数据" "D:\DICOM输出" `
  --excel "D:\结果\影像检查明细.xlsx" `
  --exceptions "D:\结果\待确认记录.csv"
```

需要完整文件级清单时：

```powershell
python .\src\dicom_organize_v2.py "D:\原始数据" "D:\DICOM输出" --manifest
```

## 读取字段

程序只读取当前需求所需的 13 个字段：

```text
PatientName
PatientID
StudyInstanceUID
SeriesInstanceUID
SOPInstanceUID
SOPClassUID
StudyDate
AcquisitionDate
Modality
SeriesDescription
PositionerMotion
NumberOfFrames
SliceThickness
```

## 类型汇总

- XA、CT、MR 直接根据 `Modality` 汇总；
- 同一 Study 有多个模态时使用顿号连接，如 `XA、CT`；
- 3D_DSA断层作为独立标志，不替代基础模态；
- 显示示例：`XA、CT（含3D_DSA断层）`。

3D 判断满足任意一组：

1. SOP Class 是标准 X-Ray 3D；
2. `XA + DYNAMIC + NumberOfFrames > 1`；
3. `XA + SliceThickness + SeriesDescription中的3D/重建关键词`。

Series 仅在运行时暂存在内存中，最终 Excel 每个 Study 只有一行。
