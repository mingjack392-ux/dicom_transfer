import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [payloadPath, outputPath, previewDir] = process.argv.slice(2);
if (!payloadPath || !outputPath) {
  throw new Error("Usage: node build_dicom_study_workbook.mjs payload.json output.xlsx [preview_dir]");
}

const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
const workbook = Workbook.create();
const main = workbook.worksheets.add("影像检查明细");
const codebook = workbook.worksheets.add("字段说明");

const headers = [
  "PatientID",
  "PatientName",
  "检查日期",
  "基础影像类型",
  "是否3D_DSA断层",
  "影像类型汇总",
  "StudyInstanceUID",
  "序列数量",
  "文件数量",
  "判断置信度",
  "判断依据",
  "来源目录",
  "转存目录",
];

main.showGridLines = false;
main.mergeCells("A1:M1");
main.getRange("A1").values = [["DICOM影像检查明细（每个Study一行）"]];
main.getRange("A1:M1").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF", size: 15 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
main.getRange("A1:M1").format.rowHeight = 30;

main.mergeCells("A2:M2");
main.getRange("A2").values = [[
  `生成时间：${payload.generated_at}    规则版本：${payload.rule_version}    ` +
  `DICOM：${payload.counters.dicom}    Study：${payload.counters.studies}`,
]];
main.getRange("A2:M2").format = {
  fill: "#D9EAF7",
  font: { color: "#1F1F1F", size: 10 },
  horizontalAlignment: "left",
  verticalAlignment: "center",
};
main.getRange("A2:M2").format.rowHeight = 22;

main.getRange("A4:M4").values = [headers];
main.getRange("A4:M4").format = {
  fill: "#4472C4",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
  borders: { preset: "all", style: "thin", color: "#B4C6E7" },
};
main.getRange("D4:F4").format = {
  fill: "#FFD966",
  font: { bold: true, color: "#000000" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
  borders: { preset: "all", style: "thin", color: "#C9B458" },
};
main.getRange("A4:M4").format.rowHeight = 32;

const dataRows = payload.studies.map((study) => [
  study.patient_id || "",
  study.patient_name || "",
  study.exam_date ? new Date(`${study.exam_date}T00:00:00`) : null,
  study.modalities || "其他/未知",
  study.has_3d || "无",
  study.display_type || "其他/未知",
  study.study_uid || "",
  study.series_count || 0,
  study.file_count || 0,
  Number(study.confidence || 0),
  study.evidence || "",
  study.source_root || "",
  study.destination_dir || "",
]);

if (dataRows.length > 0) {
  const endRow = 4 + dataRows.length;
  main.getRange(`A5:M${endRow}`).values = dataRows;
  main.getRange(`A5:M${endRow}`).format = {
    verticalAlignment: "center",
    borders: {
      insideHorizontal: { style: "thin", color: "#D9E2F3" },
      bottom: { style: "thin", color: "#D9E2F3" },
    },
  };
  main.getRange(`A5:B${endRow}`).format.horizontalAlignment = "left";
  main.getRange(`C5:C${endRow}`).format.numberFormat = "yyyy-mm-dd";
  main.getRange(`C5:J${endRow}`).format.horizontalAlignment = "center";
  main.getRange(`H5:I${endRow}`).format.numberFormat = "#,##0";
  main.getRange(`J5:J${endRow}`).format.numberFormat = "0%";
  main.getRange(`K5:M${endRow}`).format.wrapText = true;
  main.getRange(`E5:E${endRow}`).conditionalFormats.add("containsText", {
    text: "有",
    format: { fill: "#E2F0D9", font: { bold: true, color: "#006100" } },
  });
  main.getRange(`F5:F${endRow}`).conditionalFormats.add("containsText", {
    text: "其他/未知",
    format: { fill: "#FCE4D6", font: { color: "#9C0006" } },
  });
  main.getRange(`J5:J${endRow}`).conditionalFormats.add("cellIs", {
    operator: "lessThan",
    formula: 0.8,
    format: { fill: "#FFF2CC", font: { color: "#9C6500" } },
  });
  const table = main.tables.add(`A4:M${endRow}`, true, "StudySummaryTable");
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
} else {
  main.mergeCells("A5:M6");
  main.getRange("A5").values = [["没有可汇总的Study，请查看待确认记录.csv"]];
  main.getRange("A5:M6").format = {
    fill: "#FFF2CC",
    font: { color: "#9C6500" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
  };
}

main.freezePanes.freezeRows(4);
main.getRange("A:A").format.columnWidth = 16;
main.getRange("B:B").format.columnWidth = 16;
main.getRange("C:C").format.columnWidth = 13;
main.getRange("D:D").format.columnWidth = 17;
main.getRange("E:E").format.columnWidth = 19;
main.getRange("F:F").format.columnWidth = 29;
main.getRange("G:G").format.columnWidth = 42;
main.getRange("H:I").format.columnWidth = 11;
main.getRange("J:J").format.columnWidth = 13;
main.getRange("K:K").format.columnWidth = 48;
main.getRange("L:M").format.columnWidth = 36;

codebook.showGridLines = false;
codebook.mergeCells("A1:C1");
codebook.getRange("A1").values = [["字段与判断规则说明"]];
codebook.getRange("A1:C1").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF", size: 14 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
codebook.getRange("A1:C1").format.rowHeight = 28;
codebook.getRange("A3:C3").values = [["字段", "说明", "来源/规则"]];
codebook.getRange("A3:C3").format = {
  fill: "#4472C4",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#B4C6E7" },
};

const definitions = [
  ["一行的含义", "一次DICOM Study", "以StudyInstanceUID分组"],
  ["基础影像类型", "同一Study内所有Series的模态去重合并", "Modality：XA、CT、MR；多个用顿号连接"],
  ["是否3D_DSA断层", "该Study是否存在至少一个3D序列", "X-Ray 3D SOP，或XA+DYNAMIC+多帧，或XA+层厚+3D/重建关键词"],
  ["影像类型汇总", "便于人工阅读的最终类型", "示例：XA、CT（含3D_DSA断层）"],
  ["检查日期", "用于以后与手术时间匹配", "优先StudyDate，缺失时使用AcquisitionDate"],
  ["判断置信度", "当前3D或基础模态判断可靠程度", "低于80%以黄色提示，需结合待确认记录复核"],
  ["序列数量", "该Study内Series数量", "按SeriesInstanceUID去重"],
  ["文件数量", "该Study内影像实例数量", "按SOPInstanceUID去重"],
  ["下一阶段", "术中及术后6/12个月判断", "取得住院号和手术时间名单后再计算"],
];
codebook.getRange(`A4:C${3 + definitions.length}`).values = definitions;
codebook.getRange(`A4:C${3 + definitions.length}`).format = {
  verticalAlignment: "top",
  wrapText: true,
  borders: {
    insideHorizontal: { style: "thin", color: "#D9E2F3" },
    bottom: { style: "thin", color: "#D9E2F3" },
  },
};
codebook.freezePanes.freezeRows(3);
codebook.getRange("A:A").format.columnWidth = 22;
codebook.getRange("B:B").format.columnWidth = 38;
codebook.getRange("C:C").format.columnWidth = 62;

let inspected = false;
let formulaErrors = "";
if (previewDir) {
  // 预览/验收模式才做 inspect + render；日常批量转存不承担这笔固定开销。
  const inspectEnd = Math.max(6, Math.min(14, 4 + dataRows.length));
  const check = await workbook.inspect({
    kind: "table",
    range: `影像检查明细!A1:M${inspectEnd}`,
    include: "values,formulas",
    tableMaxRows: 14,
    tableMaxCols: 13,
    maxChars: 5000,
  });
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: "final formula error scan",
  });
  const mainPreview = await workbook.render({
    sheetName: "影像检查明细",
    autoCrop: "all",
    scale: 1.25,
    format: "png",
  });
  const codebookPreview = await workbook.render({
    sheetName: "字段说明",
    autoCrop: "all",
    scale: 1.25,
    format: "png",
  });
  inspected = check.ndjson.length > 0;
  formulaErrors = errors.ndjson;
  await fs.mkdir(previewDir, { recursive: true });
  await fs.writeFile(
    path.join(previewDir, "影像检查明细.png"),
    new Uint8Array(await mainPreview.arrayBuffer()),
  );
  await fs.writeFile(
    path.join(previewDir, "字段说明.png"),
    new Uint8Array(await codebookPreview.arrayBuffer()),
  );
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
// artifact-tool在大型inspect结果时可能落一个辅助NDJSON；它不是业务输出。
await fs.rm(`${outputPath}.inspect.ndjson`, { force: true });

console.log(JSON.stringify({
  output: outputPath,
  studyRows: dataRows.length,
  inspected,
  formulaErrors,
}, null, 2));
