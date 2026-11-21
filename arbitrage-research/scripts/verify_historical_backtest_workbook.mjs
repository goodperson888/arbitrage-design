import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const root = "/Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research";
const workbookPath = path.join(root, "outputs", "historical_backtest_summary_20260821", "历史拉取数据_全部回测结果_20260821.xlsx");
const previewDir = path.join(root, "outputs", "historical_backtest_summary_20260821", "previews");
const input = await FileBlob.load(workbookPath);
const wb = await SpreadsheetFile.importXlsx(input);

const checkInspect = await wb.inspect({ kind: "region", sheetId: "检查", range: "A1:F15", maxChars: 7000 });
console.log("CHECKS");
console.log(checkInspect.ndjson ?? checkInspect);

const formulaInspect = await wb.inspect({ kind: "formula", maxChars: 10000, options: { maxResults: 200 } });
console.log("FORMULAS");
console.log(formulaInspect.ndjson ?? formulaInspect);

let errorInspect;
try {
  errorInspect = await wb.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 200 },
    maxChars: 10000,
  });
  console.log("FORMULA_ERRORS");
  console.log(errorInspect.ndjson ?? errorInspect);
} catch (error) {
  console.log(`FORMULA_ERRORS_SCAN_UNAVAILABLE ${error.message}`);
}

await fs.mkdir(previewDir, { recursive: true });
const renderRanges = {
  "总览": "A1:M34",
  "核心候选": "A1:M41",
  "运行索引": "A1:V18",
  "范围结果": "A1:AE18",
  "价格分桶": "A1:AE18",
  "压力测试": "A1:AC18",
  "时间切片": "A1:AC18",
  "盘口深度": "A1:AA18",
  "失败样本": "A1:W18",
  "数据与口径": "A1:G25",
  "检查": "A1:F15",
};
for (const [sheetName, range] of Object.entries(renderRanges)) {
  const blob = await wb.render({ sheetName, range, scale: 0.7, format: "png" });
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const fileName = `${sheetName}.png`;
  await fs.writeFile(path.join(previewDir, fileName), bytes);
  console.log(`RENDERED ${sheetName} ${range} ${bytes.length}`);
}
