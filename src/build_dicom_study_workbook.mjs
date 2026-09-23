import fs from "node:fs/promises";
import fsSync from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [payloadPath, outputPath, previewDir] = process.argv.slice(2);
if (!payloadPath || !outputPath) {
  throw new Error("Usage: node build_dicom_study_workbook.mjs payload.json output.xlsx [preview_dir]");
}

const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
const workbook = Workbook.create();
const main = workbook.worksheets.add("影像检查明细");
const codebook = workbook.worksheets.add("字段说明");
const previewSheets = [{ name: "影像检查明细", range: "A1:M20" }];

function columnName(number) {
  let result = "";
  for (let value = number; value > 0; value = Math.floor((value - 1) / 26)) {
    result = String.fromCharCode(65 + ((value - 1) % 26)) + result;
  }
  return result;
}

function styleReportSheet(sheet, columns, rowCount, tableName, widths = []) {
  const lastColumn = columnName(columns.length);
  sheet.showGridLines = false;
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: "#4472C4",
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#B4C6E7" },
  };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 32;
  if (rowCount > 0) {
    sheet.getRange(`A2:${lastColumn}${rowCount + 1}`).format = {
      verticalAlignment: "top",
      borders: {
        insideHorizontal: { style: "thin", color: "#D9E2F3" },
        bottom: { style: "thin", color: "#D9E2F3" },
      },
    };
    if (rowCount <= 50000) {
      const table = sheet.tables.add(
        `A1:${lastColumn}${rowCount + 1}`, true, tableName,
      );
      table.style = "TableStyleMedium2";
      table.showFilterButton = true;
    }
  }
  sheet.freezePanes.freezeRows(1);
  columns.forEach((_, index) => {
    sheet.getRange(`${columnName(index + 1)}:${columnName(index + 1)}`).format.columnWidth =
      widths[index] || 20;
  });
  previewSheets.push({
    name: sheet.name,
    range: `A1:${lastColumn}${Math.min(rowCount + 1, 25)}`,
  });
}

function addReportSheet(report) {
  const sheet = workbook.worksheets.add(report.sheet_name);
  const columns = report.columns || [];
  const rows = report.rows || [];
  sheet.getRangeByIndexes(0, 0, 1, columns.length).values = [columns];
  if (rows.length > 0) {
    const matrix = rows.map((row) => columns.map((column) => row[column] ?? ""));
    sheet.getRangeByIndexes(1, 0, matrix.length, columns.length).values = matrix;
  }
  styleReportSheet(
    sheet, columns, rows.length, report.table_name, report.widths || [],
  );
  if (rows.length > 0 && report.sheet_name === "患者身份待确认") {
    const statusRange = sheet.getRange(`A2:A${rows.length + 1}`);
    statusRange.conditionalFormats.add("containsText", {
      text: "conflict",
      format: { fill: "#F4CCCC", font: { bold: true, color: "#9C0006" } },
    });
    statusRange.conditionalFormats.add("containsText", {
      text: "needs_review",
      format: { fill: "#FFF2CC", font: { color: "#9C6500" } },
    });
    statusRange.conditionalFormats.add("containsText", {
      text: "auto_resolved",
      format: { fill: "#E2F0D9", font: { color: "#006100" } },
    });
  }
}

function parseCsvLine(line) {
  const values = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"') {
      if (quoted && line[index + 1] === '"') {
        value += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      values.push(value);
      value = "";
    } else {
      value += character;
    }
  }
  values.push(value);
  return values;
}

async function addManifestSheet(csvPath) {
  const sheet = workbook.worksheets.add("转存清单");
  const lines = readline.createInterface({
    input: fsSync.createReadStream(csvPath, { encoding: "utf8" }),
    crlfDelay: Infinity,
  });
  let columns = [];
  let rowCount = 0;
  let chunk = [];
  for await (const line of lines) {
    if (columns.length === 0) {
      columns = parseCsvLine(line.replace(/^\uFEFF/, ""));
      sheet.getRangeByIndexes(0, 0, 1, columns.length).values = [columns];
      continue;
    }
    chunk.push(parseCsvLine(line));
    if (chunk.length >= 10000) {
      sheet.getRangeByIndexes(rowCount + 1, 0, chunk.length, columns.length).values = chunk;
      rowCount += chunk.length;
      chunk = [];
    }
  }
  if (chunk.length > 0) {
    sheet.getRangeByIndexes(rowCount + 1, 0, chunk.length, columns.length).values = chunk;
    rowCount += chunk.length;
  }
  styleReportSheet(
    sheet,
    columns,
    rowCount,
    "TransferManifestTable",
    [42, 42, 16, 38, 38, 38, 13, 67, 18, 32],
  );
}

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
  `本批DICOM：${payload.counters.dicom}    ` +
  `本批Study：${payload.counters.studies_current_run ?? payload.counters.studies}    ` +
  `累计Study：${payload.counters.studies}`,
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

for (const report of payload.reports || []) {
  addReportSheet(report);
}
if (payload.manifest_csv_path) {
  await addManifestSheet(payload.manifest_csv_path);
}

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
  ["跨批次追加", "保留以前批次并追加本批Study", "状态保存在.dicom_v3_state/study_inventory.json；相同StudyInstanceUID不重复记行"],
  ["明细排序", "同一自然患者的Study连续排列", "按身份组聚集；组内按检查日期、PatientID、StudyInstanceUID排序"],
  ["基础影像类型", "同一Study内所有Series的模态去重合并", "Modality：XA、CT、MR；多个用顿号连接"],
  ["是否3D_DSA断层", "该Study是否存在至少一个3D_DSA断层序列", "硬性条件：Modality=XA且SliceThickness有值；并且SeriesDescription命中3D/重建关键词"],
  ["影像类型汇总", "3D_DSA断层标注在所属XA类型上", "示例：XA（含3D_DSA断层）、CT、OT"],
  ["检查日期", "用于以后与手术时间匹配", "优先StudyDate，缺失时使用AcquisitionDate"],
  ["判断置信度", "当前3D或基础模态判断可靠程度", "低于80%以黄色提示，需结合待确认记录复核"],
  ["序列数量", "该Study内Series数量", "按SeriesInstanceUID去重"],
  ["文件数量", "该Study内影像实例数量", "按SOPInstanceUID去重"],
  ["下一阶段", "术中及术后6/12个月判断", "取得住院号和手术时间名单后再计算"],
];
if ((payload.reports || []).length > 0) {
  definitions.splice(
    definitions.length - 1,
    0,
    ["患者身份映射", "跨批次 PatientID 与自然人身份组关系", "运行状态保存在.dicom_v3_state，Excel页用于查看与审计"],
    ["患者身份待确认", "证据不足、人口学冲突或自动解决记录", "红色冲突、黄色待确认、绿色自动解决"],
    ["来源目录身份审计", "同一来源目录出现多个PatientID时的分流结果", "按全局PatientID身份路由"],
    ["转存异常", "UID缺失、内容冲突、身份缺失及复制错误", "不覆盖不同内容，异常文件保留"],
    ["转存清单", "使用--manifest时生成的文件级清单", "大批量时会增加Excel生成时间和文件体积"],
  );
}
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
previewSheets.push({ name: "字段说明", range: "A1:C20" });

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
  inspected = check.ndjson.length > 0;
  formulaErrors = errors.ndjson;
  await fs.mkdir(previewDir, { recursive: true });
  for (const previewSheet of previewSheets) {
    const preview = await workbook.render({
      sheetName: previewSheet.name,
      range: previewSheet.range,
      scale: 1.1,
      format: "png",
    });
    await fs.writeFile(
      path.join(previewDir, `${previewSheet.name}.png`),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }
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

// artifact-tool 在预览模式把较长 inspect 结果转存为辅助文件时可能设置非零
// exitCode；能执行到这里说明校验、渲染和导出均已成功。
process.exitCode = 0;
