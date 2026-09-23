import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";


const [workbookPath] = process.argv.slice(2);
if (!workbookPath) {
  throw new Error("Usage: node read_dicom_identity_mapping.mjs report.xlsx");
}

const input = await FileBlob.load(workbookPath);
const workbook = await SpreadsheetFile.importXlsx(input);
let values = [];
try {
  const sheet = workbook.worksheets.getItem("患者身份映射");
  const used = sheet.getUsedRange(true);
  values = used ? used.values : [];
} catch {
  values = [];
}

process.stdout.write(JSON.stringify(values || []));
