import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";


const [workbookPath] = process.argv.slice(2);
if (!workbookPath) {
  throw new Error("Usage: node read_center_workbook.mjs center.xlsx");
}

const input = await FileBlob.load(workbookPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const sheets = workbook.worksheets.items.map((sheet) => {
  const used = sheet.getUsedRange(true);
  return {
    name: sheet.name,
    values: used ? used.values : [],
  };
});

process.stdout.write(JSON.stringify(sheets));
