import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [payloadPath, outputPath, previewDir] = process.argv.slice(2);
if (!payloadPath || !outputPath) {
  throw new Error(
    "Usage: node build_center_image_workbook.mjs payload.json output.xlsx [preview_dir]",
  );
}

const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
const workbook = Workbook.create();
const headers = payload.headers || [];
const previewSheets = [];

function columnName(number) {
  let result = "";
  for (let value = number; value > 0; value = Math.floor((value - 1) / 26)) {
    result = String.fromCharCode(65 + ((value - 1) % 26)) + result;
  }
  return result;
}

function styleHeader(sheet, rangeAddress) {
  sheet.getRange(rangeAddress).format = {
    fill: "#FFFFFF",
    font: {
      bold: true,
      color: "#000000",
      name: "Times New Roman",
      size: 11,
    },
    horizontalAlignment: "left",
    verticalAlignment: "center",
    wrapText: false,
    borders: {
      insideVertical: { style: "thin", color: "#000000" },
      top: { style: "thin", color: "#000000" },
      bottom: { style: "medium", color: "#000000" },
      left: { style: "thin", color: "#000000" },
      right: { style: "thin", color: "#000000" },
    },
  };
  sheet.getRange(rangeAddress).format.rowHeight = 24;
}

function styleDataGrid(sheet, rangeAddress) {
  const range = sheet.getRange(rangeAddress);
  range.format.fill = "#FFFFFF";
  range.format.borders = {
    preset: "all",
    style: "thin",
    color: "#000000",
  };
}

function setCenterWidths(sheet) {
  const widths = [
    15, 12, 46, 46, 46, 14, 12, 48, 42, 14, 17, 12, 12, 17, 17,
  ];
  widths.forEach((width, index) => {
    const letter = columnName(index + 1);
    sheet.getRange(`${letter}:${letter}`).format.columnWidth = width;
  });
}

function addCenterSheet(center, index) {
  const sheet = workbook.worksheets.add(center.sheet_name);
  sheet.showGridLines = false;
  sheet.getRangeByIndexes(0, 0, 1, headers.length).values = [headers];
  styleHeader(sheet, `A1:${columnName(headers.length)}1`);
  const rows = center.rows || [];
  if (rows.length > 0) {
    const matrix = rows.map((row) => headers.map((header) => row[header] ?? ""));
    sheet.getRangeByIndexes(1, 0, matrix.length, headers.length).values = matrix;
    const endRow = rows.length + 1;
    sheet.getRange(`A2:O${endRow}`).format = {
      font: { name: "等线", size: 11 },
      verticalAlignment: "center",
    };
    // 标识符始终作为文本，防止Excel把UID显示为科学计数法。
    sheet.getRange(`A2:E${endRow}`).format.numberFormat = "@";
    sheet.getRange(`F2:F${endRow}`).format.numberFormat = "0";
    // 文本“术前/术中”不受数字格式影响；正数统一显示两位小数。
    sheet.getRange(`G2:G${endRow}`).format.numberFormat = "0.00";
    sheet.getRange(`J2:L${endRow}`).format.horizontalAlignment = "right";
    sheet.getRange(`N2:O${endRow}`).format.numberFormat = "0.0";
    const table = sheet.tables.add(`A1:O${endRow}`, true, `CenterImageTable${index + 1}`);
    table.style = "TableStyleLight1";
    table.showFilterButton = true;
    styleDataGrid(sheet, `A1:O${endRow}`);
    styleHeader(sheet, "A1:O1");
  }
  sheet.freezePanes.freezeRows(1);
  setCenterWidths(sheet);
  previewSheets.push({
    name: sheet.name,
    range: `A1:O${Math.min(rows.length + 1, 18)}`,
  });
}

for (const [index, center] of (payload.centers || []).entries()) {
  addCenterSheet(center, index);
}

const exceptionHeaders = [
  "中心",
  "异常类型",
  "患者",
  "住院号",
  "StudyInstanceUID",
  "SeriesUID",
  "SOPUID",
  "来源文件或行",
  "说明",
];
const exceptionSheet = workbook.worksheets.add("处理异常");
exceptionSheet.showGridLines = false;
exceptionSheet.getRange("A1:I1").values = [exceptionHeaders];
styleHeader(exceptionSheet, "A1:I1");
const exceptions = payload.exceptions || [];
if (exceptions.length > 0) {
  const values = exceptions.map((row) =>
    exceptionHeaders.map((header) => row[header] ?? ""),
  );
  exceptionSheet.getRangeByIndexes(1, 0, values.length, exceptionHeaders.length).values = values;
  const endRow = values.length + 1;
  exceptionSheet.getRange(`A2:I${endRow}`).format = {
    verticalAlignment: "top",
    wrapText: false,
  };
  exceptionSheet.getRange(`I2:I${endRow}`).format.wrapText = true;
  exceptionSheet.getRange(`A2:I${endRow}`).format.rowHeight = 32;
  exceptionSheet.getRange(`D2:G${endRow}`).format.numberFormat = "@";
  const table = exceptionSheet.tables.add(`A1:I${endRow}`, true, "ProcessingExceptionsTable");
  table.style = "TableStyleLight1";
  table.showFilterButton = true;
  styleDataGrid(exceptionSheet, `A1:I${endRow}`);
  styleHeader(exceptionSheet, "A1:I1");
}
exceptionSheet.freezePanes.freezeRows(1);
const exceptionWidths = [14, 28, 14, 18, 42, 42, 42, 54, 58];
exceptionWidths.forEach((width, index) => {
  const letter = columnName(index + 1);
  exceptionSheet.getRange(`${letter}:${letter}`).format.columnWidth = width;
});
previewSheets.push({
  name: "处理异常",
  range: `A1:I${Math.min(exceptions.length + 1, 18)}`,
});

const summarySheet = workbook.worksheets.add("运行摘要");
summarySheet.showGridLines = false;
const summaryHeaders = [
  "中心工作表",
  "中心目录名",
  "分中心表中心名称",
  "中心匹配依据",
  "扫描文件数",
  "DICOM数",
  "患者数",
  "Study数",
  "Series数",
  "输出行数",
  "已匹配行数",
  "异常数",
];
summarySheet.getRange("A1:L1").values = [summaryHeaders];
styleHeader(summarySheet, "A1:L1");
const summaryRows = (payload.centers || []).map((center) => [
  center.sheet_name,
  center.source_center_name,
  center.reference_center_name,
  center.counters?.center_match_basis || "",
  center.counters?.files_seen || 0,
  center.counters?.dicom || 0,
  center.counters?.patients || 0,
  center.counters?.studies || 0,
  center.counters?.series || 0,
  center.counters?.output_rows || 0,
  center.counters?.matched_rows || 0,
  center.counters?.exceptions || 0,
]);
if (summaryRows.length > 0) {
  summarySheet.getRangeByIndexes(1, 0, summaryRows.length, summaryHeaders.length).values = summaryRows;
  const endRow = summaryRows.length + 1;
  summarySheet.getRange(`E2:L${endRow}`).format.numberFormat = "#,##0";
  const table = summarySheet.tables.add(`A1:L${endRow}`, true, "RunSummaryTable");
  table.style = "TableStyleLight1";
  table.showFilterButton = true;
  styleDataGrid(summarySheet, `A1:L${endRow}`);
  styleHeader(summarySheet, "A1:L1");
}
summarySheet.freezePanes.freezeRows(1);
const summaryWidths = [18, 18, 28, 30, 14, 12, 12, 12, 12, 14, 16, 12];
summaryWidths.forEach((width, index) => {
  const letter = columnName(index + 1);
  summarySheet.getRange(`${letter}:${letter}`).format.columnWidth = width;
});
previewSheets.push({
  name: "运行摘要",
  range: `A1:L${Math.min(summaryRows.length + 1, 16)}`,
});

const definitionSheet = workbook.worksheets.add("字段说明");
definitionSheet.showGridLines = false;
definitionSheet.getRange("A1:C1").values = [["字段/规则", "含义", "来源或计算方法"]];
styleHeader(definitionSheet, "A1:C1");
const definitions = [
  ["中心工作表", "一个中心一个工作表", "工作表名称取转存根目录的一级中心目录名"],
  ["一行的含义", "第四批混合粒度", "单帧DICOM按Series汇总；NumberOfFrames>1按SOP输出"],
  ["住院号", "住院号/门诊号/放射号", "先限定中心，再按规范姓名匹配；住院号缺失时保持空白，不使用筛选号代填"],
  ["编号回退", "姓名不一致或缺失时的关联规则", "患者目录15_H001或01_Q001规范为15-H001或01-Q001，并与分中心表受试者筛选号唯一匹配"],
  ["患者", "患者显示名称", "优先分中心表姓名；缺失时使用姓名缩写；仍缺失时使用DICOM姓名或筛选号"],
  ["AcqusitionDate", "影像采集日期", "优先AcquisitionDate；缺失时依次使用StudyDate、SeriesDate；表头保留第四批历史拼写"],
  ["时期", "相对手术日期", "负数=术前；0=术中；正数=相差天数/30，保留两位小数"],
  ["SOPUID", "多帧对象标识", "多帧按SOP保留；单帧Series汇总行填NA"],
  ["NumberOfFrames", "多帧对象帧数", "多帧SOP读取DICOM标签；单帧Series填NA"],
  ["帧数", "单帧序列实例数", "按Series内去重后的SOPInstanceUID计数；多帧SOP填NA"],
  ["日期回退", "AcquisitionDate缺失", "依次使用StudyDate、SeriesDate，并在处理异常中记录日期来源"],
  ["日期冲突", "同一Series有多个候选日期", "主表使用当前优先级字段的最早日期，同时进入处理异常"],
  ["缺失日期", "无法计算时期", "AcquisitionDate、StudyDate和SeriesDate均缺失时时期留空"],
  ["数据安全", "只读分析", "不修改、不移动、不删除转存后DICOM及V3状态文件"],
  ["规则版本", payload.rule_version || "", `生成时间 ${payload.generated_at || ""}`],
];
definitionSheet.getRangeByIndexes(1, 0, definitions.length, 3).values = definitions;
definitionSheet.getRange(`A2:C${definitions.length + 1}`).format = {
  fill: "#FFFFFF",
  verticalAlignment: "top",
  wrapText: true,
};
styleDataGrid(definitionSheet, `A1:C${definitions.length + 1}`);
styleHeader(definitionSheet, "A1:C1");
definitionSheet.freezePanes.freezeRows(1);
definitionSheet.getRange("A:A").format.columnWidth = 23;
definitionSheet.getRange("B:B").format.columnWidth = 35;
definitionSheet.getRange("C:C").format.columnWidth = 68;
previewSheets.push({
  name: "字段说明",
  range: `A1:C${definitions.length + 1}`,
});

let inspected = false;
let formulaErrors = "";
if (previewDir) {
  const check = await workbook.inspect({
    kind: "workbook,sheet,table",
    maxChars: 8000,
    tableMaxRows: 10,
    tableMaxCols: 15,
  });
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 200 },
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
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
await fs.rm(`${outputPath}.inspect.ndjson`, { force: true });

console.log(JSON.stringify({
  output: outputPath,
  centers: (payload.centers || []).length,
  rows: (payload.centers || []).reduce((sum, center) => sum + (center.rows || []).length, 0),
  exceptions: exceptions.length,
  inspected,
  formulaErrors,
}, null, 2));
process.exitCode = 0;
