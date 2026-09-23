# DICOM 转存 V3：跨 PatientID 身份归组

入口：`src/dicom_organize_v3.py`

V3 是在 V2 安全转存能力之上新增的患者身份归组版本。V2 文件保持不变，V3
仍然复用其原子复制、SHA256 重复校验、同 UID 不同内容冲突保留、UID 缺失隔离、
Series 分类和 Study 汇总能力。

当前 `3D_DSA断层` 规则要求 `Modality=XA` 和 `SliceThickness` 有值作为硬性
条件，并要求 `SeriesDescription` 命中3D/重建关键词。DYNAMIC、多帧及 X-Ray 3D
SOP 不单独触发断层判断。

影像类型汇总将断层标注附加在XA上。例如同一Study包含XA和OT时，输出
`XA（含3D_DSA断层）、OT`，而不是`XA、OT（含3D_DSA断层）`。

“影像检查明细”优先按V3的`master_patient_key`自然患者身份组聚集，同一患者即使
存在多个PatientID，其Study也会连续排列；患者组内再按检查日期、PatientID和
StudyInstanceUID排序。没有身份映射的旧记录只使用姓名和PatientID作为显示排序回退，
不会因此改变身份判断或自动合并患者。

规则升级后，同一个 Study 在新批次中重新扫描时，新分类结果会替换状态文件中的旧
分类结果，避免旧版“动态多帧”阳性继续保留。没有被本次输入重新扫描到的历史 Study
无法自动重判；如需全部按新规则更新，应重新扫描完整原始数据。

> V3 输出路径和患者身份映射表含患者姓名、PatientID 等敏感信息。它是原始影像
> 整理程序，不等同于匿名化程序；目标目录和映射表必须放在受控存储中。

## 处理流程

V3 一次运行分两阶段，整个过程不等待人工确认：

1. 只读扫描全部 DICOM Header，收集 PatientID、PatientName、PatientBirthDate、
   PatientSex、PatientAge、InstitutionName、StudyDate 和三个 UID，建立全批次身份路由。
2. 按已生成的路由安全复制文件。证据不足的关系保持分开，并写入待确认表，不会
   阻塞剩余数据。

来源患者目录中如果混入其他患者的数据，V3 不会把该文件强行归到来源目录姓名下：

- PatientID 已在本批或历史映射中出现：按全局 PatientID 路由；
- PatientID 首次出现：使用其 DICOM Header `PatientName_PatientID` 建立新身份；
- PatientID 缺失：使用 Header 姓名，写入 `_identity_review`，并进入异常表。
- 中文来源目录名不再按“文件最多的 PatientID”分配，而是比较中文姓名拼音首字母及
  Header 英文音节顺序。首字母一致仍需符合对应音节，不能仅凭首字母归入患者。

## 同一人多 PatientID 的自动判断

自动合并采用保守规则：

- 规范化姓名必须相同；
- PatientName 末尾如果是严格的 `M-56Y`、`F 45Y` 等格式，会先拆出性别和年龄；
  例如合成姓名 `Zhang San` 与 `ZHANG SAN M-56Y^^^^` 可比较为同一姓名主体；
- 出生日期、性别、规范化医院、由 PatientAge 与 StudyDate 推算的出生年份中，至少
  两项一致；
- 任一可比较字段发生冲突时不自动合并；
- 只有姓名相同但人口学证据不足时不自动合并。

项目约定的特殊规则：同名、同性别、同医院但不同 PatientID、不同出生日期时，仍放入
同一个患者外层目录，状态记为 `grouped_needs_review`，并在“患者身份待确认”页明确记录
出生日期冲突。这是目录归组，不代表身份已经自动确认。

冲突或证据不足的组合会写入综合 Excel 的“患者身份待确认”页。人工确认安排在整批运行完成后，
不需要守着程序中途操作。

## 目标路径

普通患者只有一个 PatientID：

```text
患者姓名_PatientID/
└─ StudyInstanceUID/
   └─ SeriesInstanceUID/
      └─ SOPInstanceUID.dcm
```

确认同一自然人具有多个 PatientID：

```text
患者姓名/
├─ 患者姓名_PatientID_1/
│  └─ StudyInstanceUID/SeriesInstanceUID/SOPInstanceUID.dcm
└─ 患者姓名_PatientID_2/
   └─ StudyInstanceUID/SeriesInstanceUID/SOPInstanceUID.dcm
```

如果存在同名但确认不是同一人的多个身份组，多 PatientID 组的外层目录会增加稳定组号，
例如 `王某某__G000123`，避免两个自然人的目录发生碰撞。

## 运行命令

最简运行：

```powershell
python .\src\dicom_organize_v3.py "D:\输入目录" "D:\输出目录"
```

本地 D 盘这类单盘输入建议使用 1 个 Header 解析线程和 2 个复制线程：

```powershell
python .\src\dicom_organize_v3.py `
  "D:\输入目录" `
  "D:\输出目录" `
  --workers 1 `
  --copy-workers 2 `
  --manifest
```

普通 Linux 服务器请使用独立入口，它不依赖 Node.js、MJS 或 Codex 私有模块：

```bash
python3 -m pip install --user -r requirements-linux.txt

python3 ./src/dicom_organize_v3_linux.py "/data/input" "/data/output" \
  --workers 1 --copy-workers 2 --manifest
```

Linux 入口启动时会先检查 `openpyxl`。依赖缺失时立即退出，不会等扫描、复制完成后
才报 Excel 错误。

如果影像已经由旧入口复制完成，只在最后生成 Excel 时因 Node.js/MJS 失败，可直接从
`.dicom_v3_state` 补报表，不重新扫描、不复制 DICOM：

```bash
python3 ./src/dicom_organize_v3_linux.py "/data/input" "/data/output" --report-only
```

旧失败任务通常已经写入 `identity_mapping.json` 和 `study_inventory.json`，因此可以直接
使用该命令。如果两份状态也不存在，程序会明确报错而不会生成空报表。

## 输出文件

默认只生成一个业务报表 `影像检查明细_V3.xlsx`，其中包含：

- 影像检查明细；
- 患者身份映射；
- 患者身份待确认；
- 来源目录身份审计；
- 转存异常；
- 字段说明；
- 转存清单（仅使用 `--manifest` 时）。

程序运行状态保存在目标目录的 `.dicom_v3_state/identity_mapping.json` 和
`.dicom_v3_state/study_inventory.json`，避免每次从 Excel 反读造成固定开销，并确保
“影像检查明细”跨批次按 StudyInstanceUID 追加、重复运行不重复记行。首次升级时若还没有
Study 状态文件，会自动从现有综合 Excel 接管历史明细。状态文件不是数据库，也不应删除。
综合 Excel 和状态文件都包含敏感身份信息，应放在受控存储中。

如需兼容旧流程，可加 `--keep-csv` 同时保留原来的 CSV；显式传入 `--identity-map`
仍支持已有 CSV。V3 第一次遇到旧版 `患者身份映射表.csv` 时会自动导入。

## 重要边界

- V3 不修改 DICOM Header，也不做匿名化。
- V3 递归读取普通文件，但不在运行中解压 ZIP；压缩数据应先解压到输入目录。
- V3 不自动移动旧版本已经生成的目录。首次启用 V3 建议使用空目标目录；后续批次使用
  同一目标目录和同一身份映射表。
- 如果某个原本单 PatientID 的自然人在以后批次发现第二个 PatientID，后续路径将采用
  多 PatientID 嵌套结构；此前已经存在的平铺目录需要在人工确认后单独迁移。
- 删除 `.dicom_v3_state` 会失去跨批次身份稳定性；综合 Excel 只是可读审计报表。
- `--manifest` 会增加 Excel 生成时间和文件体积；不需要逐文件清单时可省略。
- 本机样本测试中 8 个 Header 线程比 1 个线程慢；机械盘或单盘场景不要盲目增加线程。

## 测试

```powershell
python -m unittest tests.test_dicom_organize_v3 -v
python -m unittest discover -s tests -p "test_*.py" -v
```
