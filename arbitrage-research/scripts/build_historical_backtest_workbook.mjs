import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const outputDir = path.join(root, "outputs", "historical_backtest_summary_20260821");
const outputPath = path.join(outputDir, "历史拉取数据_全部回测结果_20260821.xlsx");

const COLORS = {
  navy: "#16324F",
  blue: "#1F4E78",
  teal: "#0F6B78",
  paleBlue: "#DDEBF7",
  paleTeal: "#DDEFEF",
  paleYellow: "#FFF2CC",
  paleGreen: "#E2F0D9",
  paleRed: "#FCE4D6",
  gray: "#F2F2F2",
  darkGray: "#595959",
  white: "#FFFFFF",
  border: "#D9E2F3",
  green: "#548235",
  red: "#C00000",
};

function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        field += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += ch;
    }
  }
  if (field.length || row.length) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }
  return rows.filter((r) => r.some((v) => v !== ""));
}

function scalar(value) {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "string") return value;
  if (value === "True" || value === "true") return true;
  if (value === "False" || value === "false") return false;
  if (/^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(value)) return Number(value);
  return value;
}

async function readCsv(relativePath) {
  const text = await fs.readFile(path.join(root, relativePath), "utf8");
  const matrix = parseCsv(text);
  const headers = matrix[0];
  return matrix.slice(1).map((row) => Object.fromEntries(headers.map((h, i) => [h, scalar(row[i] ?? "")])))
}

function sourceFamily(runId, priceSource) {
  if (priceSource === "trade") return "官方成交";
  if (runId === "last_window_70_88") return "早期试跑快照";
  return "连续卖一快照";
}

function datasetLabel(runId, count) {
  if (runId.includes("same47")) return "same47";
  if (runId.includes("7d_hourly") || runId.startsWith("week168")) return "week168";
  if (runId.includes("200") || runId.startsWith("day200")) return "recent200";
  if (runId.includes("combined")) return "combined351";
  if (runId.includes("smoke")) return "smoke3";
  if (runId.startsWith("l3_") || runId.includes("complete")) return "snapshot47";
  return `sample${count ?? ""}`;
}

function phaseLabel(runId) {
  if (runId.includes("remaining_100_0") || runId.includes("remaining_120_0")) return "补测";
  if (runId.includes("smoke")) return "冒烟验证";
  if (runId === "last_window_70_88") return "早期试跑";
  return "主历史回测";
}

function toA1Column(index) {
  let n = index + 1;
  let out = "";
  while (n) {
    n -= 1;
    out = String.fromCharCode(65 + (n % 26)) + out;
    n = Math.floor(n / 26);
  }
  return out;
}

function writeMatrix(sheet, startRow, startCol, matrix) {
  if (!matrix.length || !matrix[0].length) return;
  sheet.getRangeByIndexes(startRow, startCol, matrix.length, matrix[0].length).values = matrix;
}

function setTitle(sheet, title, subtitle, lastCol) {
  sheet.showGridLines = false;
  const end = toA1Column(lastCol);
  sheet.getRange(`A1:${end}1`).merge();
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${end}1`).format = {
    fill: COLORS.navy,
    font: { color: COLORS.white, bold: true, size: 18 },
    verticalAlignment: "center",
  };
  sheet.getRange("A1").format.rowHeight = 30;
  sheet.getRange(`A2:${end}2`).merge();
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange(`A2:${end}2`).format = {
    fill: COLORS.paleBlue,
    font: { color: COLORS.darkGray, italic: true, size: 10 },
    wrapText: true,
    verticalAlignment: "center",
  };
  sheet.getRange("A2").format.rowHeight = 30;
}

function formatHeader(range) {
  range.format = {
    fill: COLORS.teal,
    font: { color: COLORS.white, bold: true },
    wrapText: true,
    verticalAlignment: "center",
    horizontalAlignment: "center",
    borders: { preset: "all", style: "thin", color: COLORS.border },
  };
  range.format.rowHeight = 32;
}

function formatDataBlock(sheet, headerRow1Based, headers, rowCount, options = {}) {
  const endCol = toA1Column(headers.length - 1);
  const endRow = headerRow1Based + rowCount;
  formatHeader(sheet.getRange(`A${headerRow1Based}:${endCol}${headerRow1Based}`));
  if (rowCount > 0) {
    const body = sheet.getRange(`A${headerRow1Based + 1}:${endCol}${endRow}`);
    body.format = {
      borders: { preset: "all", style: "thin", color: COLORS.border },
      verticalAlignment: "center",
    };
    body.format.rowHeight = options.rowHeight ?? 18;
    const table = sheet.tables.add(`A${headerRow1Based}:${endCol}${endRow}`, true, options.tableName);
    table.style = options.tableStyle ?? "TableStyleMedium2";
    table.showBandedRows = true;
    table.showFilterButton = true;
  }
  sheet.freezePanes.freezeRows(headerRow1Based);
}

function applyPercent(sheet, columnIndex, startRow, endRow) {
  if (endRow < startRow) return;
  const col = toA1Column(columnIndex);
  sheet.getRange(`${col}${startRow}:${col}${endRow}`).format.numberFormat = "0.00%";
}

function applyDecimal(sheet, columnIndex, startRow, endRow, format = "0.0000") {
  if (endRow < startRow) return;
  const col = toA1Column(columnIndex);
  sheet.getRange(`${col}${startRow}:${col}${endRow}`).format.numberFormat = format;
}

function applyPnlConditional(sheet, columnIndex, startRow, endRow) {
  if (endRow < startRow) return;
  const col = toA1Column(columnIndex);
  const range = sheet.getRange(`${col}${startRow}:${col}${endRow}`);
  range.conditionalFormats.add("cellIs", { operator: "greaterThan", formula: 0, format: { font: { color: COLORS.green, bold: true }, fill: COLORS.paleGreen } });
  range.conditionalFormats.add("cellIs", { operator: "lessThan", formula: 0, format: { font: { color: COLORS.red, bold: true }, fill: COLORS.paleRed } });
}

const runsDir = path.join(root, "runs");
const runEntries = await fs.readdir(runsDir, { withFileTypes: true });
const reportPaths = [];
for (const entry of runEntries) {
  if (!entry.isDirectory()) continue;
  const p = path.join(runsDir, entry.name, "last_window_report.json");
  try {
    await fs.access(p);
    reportPaths.push(p);
  } catch {
    // Not a last-window report directory.
  }
}
reportPaths.sort();

const runIndex = [];
const rangeResults = [];
const bucketResults = [];
for (const reportPath of reportPaths) {
  const report = JSON.parse(await fs.readFile(reportPath, "utf8"));
  const stat = await fs.stat(reportPath);
  const runId = path.basename(path.dirname(reportPath));
  const params = report.parameters ?? {};
  const dataset = report.dataset ?? {};
  const priceSource = params.priceSource ?? "best_ask";
  const windows = (params.windowsSeconds ?? []).join("/");
  const minRemaining = params.minRemainingSeconds ?? 0;
  const resultRows = report.results ?? [];
  const coverage = report.coverage ?? [];
  const coverageComplete = coverage.filter((r) => r.complete).length;
  runIndex.push({
    run_id: runId,
    phase: phaseLabel(runId),
    source_family: sourceFamily(runId, priceSource),
    dataset: datasetLabel(runId, dataset.definedMarketCount),
    price_source: priceSource,
    market_count: dataset.definedMarketCount ?? null,
    settled_market_count: dataset.settledMarketCount ?? null,
    windows_seconds: windows,
    min_remaining_seconds: minRemaining,
    price_min: scalar(params.priceMin),
    price_max: scalar(params.priceMax),
    bucket_size: scalar(params.bucketSize),
    fee_model: params.feeModel ?? null,
    fee_rate: scalar(params.feeRate),
    max_quote_gap_seconds: scalar(params.maxQuoteGapSeconds),
    coverage_rows: coverage.length,
    coverage_complete_rows: coverageComplete,
    result_rows: resultRows.length,
    provisional: Boolean(report.provisional),
    report_modified_at: stat.mtime,
    report_path: path.relative(root, reportPath),
    result_path: path.relative(root, path.join(path.dirname(reportPath), "last_window_results.csv")),
  });
  for (const r of resultRows) {
    const enriched = {
      run_id: runId,
      phase: phaseLabel(runId),
      source_family: sourceFamily(runId, priceSource),
      dataset: datasetLabel(runId, dataset.definedMarketCount),
      price_source: priceSource,
      fee_model: params.feeModel ?? null,
      min_remaining_seconds: minRemaining,
      ...r,
    };
    for (const [k, v] of Object.entries(enriched)) enriched[k] = scalar(v);
    if (r.bucketType === "RANGE") rangeResults.push(enriched);
    else bucketResults.push(enriched);
  }
}

const practicalRows = await readCsv("runs/practical_candidates_90_30.csv");
const practicalWideRows = [];
for (const [windowSeconds, sourceFile] of [
  [100, "runs/practical_candidates_100_0.csv"],
  [120, "runs/practical_candidates_120_0.csv"],
]) {
  const rows = await readCsv(sourceFile);
  for (const row of rows) practicalWideRows.push({ window_seconds: windowSeconds, source_file: sourceFile, ...row });
}

const failureRows = [];
for (const [candidate, sourceFile] of [
  ["DOWN 0.80–0.85 size≥10 90–30秒", "runs/down_80_85_size10_90_30_failure_analysis.json"],
  ["UP 0.80–0.85 size≥10 90–30秒", "runs/up_80_85_size10_90_30_failure_analysis.json"],
]) {
  const doc = JSON.parse(await fs.readFile(path.join(root, sourceFile), "utf8"));
  for (const f of doc.failures ?? []) {
    failureRows.push({
      candidate,
      source_file: sourceFile,
      market_id: f.marketId,
      official_winner: f.winner,
      entry_time_utc: f.entryTimeUtc ? new Date(f.entryTimeUtc) : null,
      entry_remaining_seconds: scalar(f.entryRemainingSeconds),
      entry_price: scalar(f.entryPrice),
      observed_trade_size: scalar(f.entryObservedTradeSize),
      entry_fee_per_share: scalar(f.entryFeePerShare),
      target_min_after_entry: scalar(f.targetPriceAfterEntry?.minimum),
      target_max_after_entry: scalar(f.targetPriceAfterEntry?.maximum),
      target_last_price: scalar(f.targetPriceAfterEntry?.last),
      target_last_remaining_seconds: scalar(f.targetPriceAfterEntry?.lastRemainingSeconds),
      opposing_last_price: scalar(f.opposingLastPrice),
      opposing_last_remaining_seconds: scalar(f.opposingLastRemainingSeconds),
      first_target_below_half_remaining_seconds: scalar(f.firstTargetBelowHalfRemainingSeconds),
      classification: f.classification,
      trade_history_complete: Boolean(f.dataChecks?.tradeHistoryComplete),
      duplicate_event_id_count: f.dataChecks?.duplicateEventIdCount ?? null,
      timestamps_second_precision: Boolean(f.dataChecks?.timestampsSecondPrecision),
      candidate_fill_count: doc.summary?.fillCount ?? null,
      candidate_loss_count: doc.summary?.lossCount ?? null,
      confirmed_terminal_reversals: doc.summary?.confirmedTerminalReversals ?? null,
    });
  }
}
const supplementRunIds = [
  "btc_5m_official_trades_80_85_remaining_90_0_fee",
  "btc_5m_official_trades_80_85_remaining_100_0_fee",
  "btc_5m_official_trades_80_85_remaining_120_0_fee",
  "btc_5m_official_trades_80_85_remaining_120_60_fee",
];
const supplementRows = rangeResults
  .filter((r) => supplementRunIds.includes(r.run_id) && r.bucket === "0.80-0.85")
  .sort((a, b) => (a.windowSeconds - b.windowSeconds) || (a.min_remaining_seconds - b.min_remaining_seconds) || String(a.outcome).localeCompare(String(b.outcome)));

const pressureSources = [
  ["主压力矩阵", "runs/candidate_stress_75_80_80_85.csv"],
  ["same47交叉验证", "runs/candidate_stress_same47.csv"],
];
const timeSources = [
  ["70/30时间切片", "runs/candidate_stress_chrono_70_30.csv"],
  ["7时段稳定性", "runs/candidate_stress_chrono_7blocks.csv"],
];
const depthSources = [
  ["L4原始压力", "runs/l4_depth_stress_75_80_80_85.csv"],
  ["L4覆盖门槛后", "runs/l4_depth_stress_coverage_gated.csv"],
];

async function combineSources(sourceDefs) {
  const combined = [];
  for (const [sourceName, sourcePath] of sourceDefs) {
    const rows = await readCsv(sourcePath);
    for (const row of rows) combined.push({ source_group: sourceName, source_file: sourcePath, ...row });
  }
  return combined;
}

const pressureRows = await combineSources(pressureSources);
const timeRows = await combineSources(timeSources);
const depthRows = await combineSources(depthSources);

const wb = Workbook.create();
const sheets = {
  overview: wb.worksheets.add("总览"),
  core: wb.worksheets.add("核心候选"),
  runs: wb.worksheets.add("运行索引"),
  ranges: wb.worksheets.add("范围结果"),
  buckets: wb.worksheets.add("价格分桶"),
  pressure: wb.worksheets.add("压力测试"),
  time: wb.worksheets.add("时间切片"),
  depth: wb.worksheets.add("盘口深度"),
  failures: wb.worksheets.add("失败样本"),
  sources: wb.worksheets.add("数据与口径"),
  checks: wb.worksheets.add("检查"),
};

// 核心候选
setTitle(sheets.core, "核心候选与补测窗口", "所有内容均来自历史拉取数据；未纳入实时纸上交易。90–30 秒候选与 90/100/120 秒宽窗口回测不是同一策略口径。", 12);
sheets.core.getRange("A4:M4").merge();
sheets.core.getRange("A4").values = [["A. 可执行候选（排除最后 30 秒）"]];
sheets.core.getRange("A4:M4").format = { fill: COLORS.blue, font: { color: COLORS.white, bold: true, size: 12 } };
const coreHeaders = ["候选", "样本集", "市场数", "成交数", "胜数", "败数", "胜率", "均价", "净收益/份", "总净收益", "手续费模型", "执行口径", "结论标记"];
writeMatrix(sheets.core, 4, 0, [coreHeaders]);
const coreValues = practicalRows.map((r) => [
  r.candidate, r.dataset, r.market_count, r.fill_count, r.win_count, null, r.win_rate,
  r.average_entry_price, r.net_pnl_per_share, null, r.fee_model, r.execution_note,
  r.net_pnl_per_share > 0 ? "正收益" : "负收益",
]);
writeMatrix(sheets.core, 5, 0, coreValues);
const coreStart = 6;
const coreEnd = coreStart + coreValues.length - 1;
sheets.core.getRange(`F${coreStart}`).formulas = [[`=D${coreStart}-E${coreStart}`]];
sheets.core.getRange(`F${coreStart}:F${coreEnd}`).fillDown();
sheets.core.getRange(`J${coreStart}`).formulas = [[`=D${coreStart}*I${coreStart}`]];
sheets.core.getRange(`J${coreStart}:J${coreEnd}`).fillDown();
formatDataBlock(sheets.core, 5, coreHeaders, coreValues.length, { tableName: "CoreCandidatesTable", tableStyle: "TableStyleMedium4" });
applyPercent(sheets.core, 6, coreStart, coreEnd);
applyDecimal(sheets.core, 7, coreStart, coreEnd, "0.0000");
applyDecimal(sheets.core, 8, coreStart, coreEnd, "+0.0000;-0.0000;0.0000");
applyDecimal(sheets.core, 9, coreStart, coreEnd, "+0.000;-0.000;0.000");
applyPnlConditional(sheets.core, 8, coreStart, coreEnd);
applyPnlConditional(sheets.core, 9, coreStart, coreEnd);

const practicalWideTitleRow = coreEnd + 3;
sheets.core.getRange(`A${practicalWideTitleRow}:M${practicalWideTitleRow}`).merge();
sheets.core.getRange(`A${practicalWideTitleRow}`).values = [["B. 100/120 秒补测候选（含 size≥10 对照）"]];
sheets.core.getRange(`A${practicalWideTitleRow}:M${practicalWideTitleRow}`).format = { fill: COLORS.paleYellow, font: { color: COLORS.darkGray, bold: true, size: 11 } };
const wideHeaderRow = practicalWideTitleRow + 1;
const wideHeaders = ["条件", "窗口秒", "方向", "市场数", "成交数", "胜数", "败数", "胜率", "均价", "盈亏平衡胜率", "净收益/份", "手续费模型", "执行口径"];
writeMatrix(sheets.core, wideHeaderRow - 1, 0, [wideHeaders]);
const wideValues = practicalWideRows.map((r) => [
  r.condition, r.window_seconds, r.outcome, r.market_count, r.fill_count, r.win_count, null,
  r.win_rate, r.average_entry_price, r.breakeven_win_rate, r.net_pnl_per_share, r.fee_model, r.execution_note,
]);
writeMatrix(sheets.core, wideHeaderRow, 0, wideValues);
const wideStart = wideHeaderRow + 1;
const wideEnd = wideHeaderRow + wideValues.length;
sheets.core.getRange(`G${wideStart}`).formulas = [[`=E${wideStart}-F${wideStart}`]];
sheets.core.getRange(`G${wideStart}:G${wideEnd}`).fillDown();
formatDataBlock(sheets.core, wideHeaderRow, wideHeaders, wideValues.length, { tableName: "PracticalWideTable", tableStyle: "TableStyleMedium4" });
applyPercent(sheets.core, 7, wideStart, wideEnd);
applyDecimal(sheets.core, 8, wideStart, wideEnd, "0.0000");
applyPercent(sheets.core, 9, wideStart, wideEnd);
applyDecimal(sheets.core, 10, wideStart, wideEnd, "+0.0000;-0.0000;0.0000");
applyPnlConditional(sheets.core, 10, wideStart, wideEnd);

const supplementTitleRow = wideEnd + 3;
sheets.core.getRange(`A${supplementTitleRow}:M${supplementTitleRow}`).merge();
sheets.core.getRange(`A${supplementTitleRow}`).values = [["C. 0.80–0.85 标准宽窗口结果（未加 size≥10/路径过滤）"]];
sheets.core.getRange(`A${supplementTitleRow}:M${supplementTitleRow}`).format = { fill: COLORS.paleYellow, font: { color: COLORS.darkGray, bold: true, size: 11 } };
const supHeaderRow = supplementTitleRow + 1;
const supHeaders = ["运行", "阶段", "最大剩余秒", "最小剩余秒", "方向", "市场数", "成交数", "胜数", "败数", "胜率", "均价", "盈亏平衡胜率", "净收益/份"];
writeMatrix(sheets.core, supHeaderRow - 1, 0, [supHeaders]);
const supValues = supplementRows.map((r) => [
  r.run_id, r.phase, r.windowSeconds, r.min_remaining_seconds, r.outcome,
  r.eligibleSettledMarketCount, r.settledTradeCount, r.winCount, r.lossCount,
  r.winRate, r.averageEntryPrice, r.breakevenWinRate, r.netPnlPerShare,
]);
writeMatrix(sheets.core, supHeaderRow, 0, supValues);
formatDataBlock(sheets.core, supHeaderRow, supHeaders, supValues.length, { tableName: "SupplementWindowsTable", tableStyle: "TableStyleMedium2" });
const supStart = supHeaderRow + 1;
const supEnd = supHeaderRow + supValues.length;
applyPercent(sheets.core, 9, supStart, supEnd);
applyDecimal(sheets.core, 10, supStart, supEnd, "0.0000");
applyPercent(sheets.core, 11, supStart, supEnd);
applyDecimal(sheets.core, 12, supStart, supEnd, "+0.0000;-0.0000;0.0000");
applyPnlConditional(sheets.core, 12, supStart, supEnd);
sheets.core.getRange("A:M").format.wrapText = false;
sheets.core.getRange("A:A").format.columnWidth = 48;
sheets.core.getRange("B:B").format.columnWidth = 18;
sheets.core.getRange("C:J").format.columnWidth = 13;
sheets.core.getRange("K:K").format.columnWidth = 24;
sheets.core.getRange("L:L").format.columnWidth = 44;
sheets.core.getRange("M:M").format.columnWidth = 14;

// 运行索引
const runHeaders = ["run_id", "阶段", "数据类型", "样本集", "价格源", "市场数", "已结算", "最大窗口秒", "最小剩余秒", "价格下限", "价格上限", "分桶", "手续费模型", "费率", "最大报价缺口秒", "覆盖行", "完整覆盖行", "结果行", "临时结果", "报告修改时间", "报告路径", "结果路径"];
const runValues = runIndex.map((r) => [
  r.run_id, r.phase, r.source_family, r.dataset, r.price_source, r.market_count, r.settled_market_count,
  r.windows_seconds, r.min_remaining_seconds, r.price_min, r.price_max, r.bucket_size, r.fee_model, r.fee_rate,
  r.max_quote_gap_seconds, r.coverage_rows, r.coverage_complete_rows, r.result_rows, r.provisional,
  r.report_modified_at, r.report_path, r.result_path,
]);
setTitle(sheets.runs, "历史回测运行索引", "每行对应一个 runs/*/last_window_report.json；包括刚补测的 100/120 秒，排除实时监控与纸上交易。", runHeaders.length - 1);
writeMatrix(sheets.runs, 3, 0, [runHeaders, ...runValues]);
formatDataBlock(sheets.runs, 4, runHeaders, runValues.length, { tableName: "RunIndexTable" });
const runEnd = 4 + runValues.length;
sheets.runs.getRange(`T5:T${runEnd}`).format.numberFormat = "yyyy-mm-dd hh:mm:ss";
sheets.runs.getRange("A:A").format.columnWidth = 48;
sheets.runs.getRange("B:E").format.columnWidth = 18;
sheets.runs.getRange("F:S").format.columnWidth = 14;
sheets.runs.getRange("T:T").format.columnWidth = 21;
sheets.runs.getRange("U:V").format.columnWidth = 60;

const resultHeaders = [
  "run_id", "阶段", "数据类型", "样本集", "价格源", "手续费模型", "最小剩余秒",
  "最大窗口秒", "分桶类型", "分桶", "价格下限", "价格上限", "方向", "可用结算市场", "完整覆盖市场",
  "成交数", "无信号市场", "胜数", "败数", "胜率", "95%CI下限", "95%CI上限", "基准方向胜率", "相对基准提升",
  "机会率", "平均入场价", "盈亏平衡胜率", "总净收益", "净收益/份", "未结算成交", "临时结果",
];
function resultValues(rows) {
  return rows.map((r) => [
    r.run_id, r.phase, r.source_family, r.dataset, r.price_source, r.fee_model, r.min_remaining_seconds,
    r.windowSeconds, r.bucketType, r.bucket, r.priceMin, r.priceMax, r.outcome, r.eligibleSettledMarketCount,
    r.completeCoverageMarketCount, r.settledTradeCount, r.noSignalMarketCount, r.winCount, r.lossCount, r.winRate,
    r.winRateCi95Low, r.winRateCi95High, r.baselineOutcomeWinRate, r.winRateLiftVsUnconditional, r.opportunityRate,
    r.averageEntryPrice, r.breakevenWinRate, r.totalNetPnl, r.netPnlPerShare, r.unsettledFillCount, r.provisional,
  ]);
}
function buildResultSheet(sheet, title, subtitle, rows, tableName) {
  const values = resultValues(rows);
  setTitle(sheet, title, subtitle, resultHeaders.length - 1);
  writeMatrix(sheet, 3, 0, [resultHeaders, ...values]);
  formatDataBlock(sheet, 4, resultHeaders, values.length, { tableName, rowHeight: 17 });
  const end = 4 + values.length;
  for (const idx of [19, 20, 21, 22, 23, 24, 26]) applyPercent(sheet, idx, 5, end);
  for (const idx of [25, 27, 28]) applyDecimal(sheet, idx, 5, end, idx === 25 ? "0.0000" : "+0.0000;-0.0000;0.0000");
  applyPnlConditional(sheet, 27, 5, end);
  applyPnlConditional(sheet, 28, 5, end);
  sheet.getRange("A:A").format.columnWidth = 46;
  sheet.getRange("B:G").format.columnWidth = 17;
  sheet.getRange("H:AE").format.columnWidth = 14;
}
buildResultSheet(sheets.ranges, "全部范围级结果", "包含每一组回测的 RANGE 行，适合比较价格区间、时间窗口、方向、样本集与价格源。", rangeResults, "RangeResultsTable");
buildResultSheet(sheets.buckets, "全部价格分桶结果", "包含每一组回测的 PRICE_BUCKET 行；与“范围结果”合计后即为所有 last-window 输出行。", bucketResults, "BucketResultsTable");

function unionHeaders(rows) {
  const headers = [];
  const seen = new Set();
  for (const row of rows) for (const key of Object.keys(row)) if (!seen.has(key)) { seen.add(key); headers.push(key); }
  return headers;
}
function buildUnionSheet(sheet, title, subtitle, rows, tableName) {
  const headers = unionHeaders(rows);
  const values = rows.map((r) => headers.map((h) => r[h] ?? null));
  setTitle(sheet, title, subtitle, headers.length - 1);
  writeMatrix(sheet, 3, 0, [headers, ...values]);
  formatDataBlock(sheet, 4, headers, values.length, { tableName, rowHeight: 17 });
  const end = 4 + values.length;
  for (const name of ["winRate", "winRateCi95Low", "winRateCi95High", "opportunityRate", "breakevenWinRate"]) {
    const idx = headers.indexOf(name);
    if (idx >= 0) applyPercent(sheet, idx, 5, end);
  }
  for (const name of ["netPnlPerShare", "totalNetPnl"]) {
    const idx = headers.indexOf(name);
    if (idx >= 0) {
      applyDecimal(sheet, idx, 5, end, "+0.0000;-0.0000;0.0000");
      applyPnlConditional(sheet, idx, 5, end);
    }
  }
  sheet.getRange("A:A").format.columnWidth = 22;
  sheet.getRange("B:B").format.columnWidth = 58;
  if (headers.length > 2) sheet.getRange(`C:${toA1Column(headers.length - 1)}`).format.columnWidth = 15;
  return { headers, end };
}
buildUnionSheet(sheets.pressure, "候选压力测试全表", "主 351/200/168 市场压力矩阵 + same47 交叉验证；包含路径、延迟、连续成交、最小成交量等条件。", pressureRows, "PressureTestsTable");
buildUnionSheet(sheets.time, "时间稳定性全表", "70/30 时间切片与 7 时段分块结果，用于观察候选是否只在某一段历史有效。", timeRows, "TimeSlicesTable");
buildUnionSheet(sheets.depth, "盘口深度与覆盖门槛", "L4 数量/VWAP 压力结果及覆盖门槛结果。覆盖合格市场为 0 时，零成交不能解释成策略无机会。", depthRows, "DepthTestsTable");
const failureSheetMeta = buildUnionSheet(sheets.failures, "历史失败样本与反转分类", "DOWN/UP 0.80–0.85、size≥10、90–30 秒候选的全部失败笔；仅使用历史成交与官方结算，不含实时纸上交易。", failureRows, "FailureCasesTable");
const failureTimeIdx = failureSheetMeta.headers.indexOf("entry_time_utc");
if (failureTimeIdx >= 0) sheets.failures.getRange(`${toA1Column(failureTimeIdx)}5:${toA1Column(failureTimeIdx)}${failureSheetMeta.end}`).format.numberFormat = "yyyy-mm-dd hh:mm:ss";
for (const name of ["entry_price", "observed_trade_size", "entry_fee_per_share", "target_min_after_entry", "target_max_after_entry", "target_last_price", "opposing_last_price"]) {
  const idx = failureSheetMeta.headers.indexOf(name);
  if (idx >= 0) applyDecimal(sheets.failures, idx, 5, failureSheetMeta.end, "0.0000");
}

// 数据与口径
setTitle(sheets.sources, "数据来源、执行口径与排除范围", "用于后续策略交叉验证时复用；路径均相对于 arbitrage-research 项目根目录。", 6);
const sourceHeaders = ["类别", "数据/结果文件", "用途", "市场/行数", "精度或口径", "本次是否纳入", "注意事项"];
const sourceValues = [
  ["官方成交", "data/markets/btc_5m_official_trades_combined.ndjson", "351 市场主历史成交样本", 351, "成交时间戳为秒级；信号不等于盘口可成交", "是", "主样本；UP 186 / DOWN 165"],
  ["官方成交", "data/markets/btc_5m_official_trades_200.ndjson", "最近 200 市场稳健性", 200, "成交时间戳为秒级", "是", "recent200"],
  ["官方成交", "data/markets/btc_5m_official_trades_7d_hourly.ndjson", "7 天每小时抽样", 168, "成交时间戳为秒级", "是", "week168"],
  ["官方成交", "data/markets/btc_5m_official_trades_smoke.ndjson", "接口与流程冒烟验证", 3, "非正式统计样本", "是（已标记）", "不可用作策略结论"],
  ["连续卖一", "data/markets/btc_5m_complete_official_settlement.ndjson", "47 市场连续卖一验证", 47, "快照高频，max quote gap 口径见各运行", "是", "与官方成交的执行含义不同"],
  ["L4 盘口", "runs/l4_depth_stress_75_80_80_85.csv", "数量、VWAP、延迟压力", depthRows.filter((r) => r.source_group === "L4原始压力").length, "需要覆盖合格才能解释", "是", "40 个独立市场，5 秒覆盖门槛下合格数为 0"],
  ["候选汇总", "runs/practical_candidates_90_30.csv", "90–30 秒可执行候选", practicalRows.length, "排除最后 30 秒", "是", "observed trade signal，不保证真实盘口成交"],
  ["候选补测", "runs/practical_candidates_100_0.csv", "最后 100 秒，含 size≥10 对照", practicalWideRows.filter((r) => r.window_seconds === 100).length, "包含最后 30 秒", "是", "DOWN size≥10 接近盈亏平衡但仍略负"],
  ["候选补测", "runs/practical_candidates_120_0.csv", "最后 120 秒，含 size≥10 对照", practicalWideRows.filter((r) => r.window_seconds === 120).length, "包含最后 30 秒", "是", "DOWN size≥10 小幅正收益，弱于 90–30 秒"],
  ["失败分析", "runs/*_80_85_size10_90_30_failure_analysis.json", "UP/DOWN 全部失败笔与终局反转分类", failureRows.length, "秒级成交时间戳", "是", "DOWN 6 败中 5 笔确认终局反转；UP 12 败中 10 笔"],
  ["补测", "runs/btc_5m_official_trades_80_85_remaining_100_0_fee", "0.80–0.85，最后 100 秒", supplementRows.filter((r) => r.windowSeconds === 100).length, "未加 size≥10 / 路径过滤", "是", "含最后 30 秒"],
  ["补测", "runs/btc_5m_official_trades_80_85_remaining_120_0_fee", "0.80–0.85，最后 120 秒", supplementRows.filter((r) => r.windowSeconds === 120 && r.min_remaining_seconds === 0).length, "未加 size≥10 / 路径过滤", "是", "含最后 30 秒"],
  ["实时纸上交易", "data/live_monitor/**; data/monitor/paper_trades.json", "实时监控、纸上成交与逐秒/逐消息记录", null, "实时流", "否", "用户指定排除；不能与历史回测混合"],
];
writeMatrix(sheets.sources, 3, 0, [sourceHeaders, ...sourceValues]);
formatDataBlock(sheets.sources, 4, sourceHeaders, sourceValues.length, { tableName: "SourcesTable", tableStyle: "TableStyleMedium4", rowHeight: 30 });
sheets.sources.getRange("A:A").format.columnWidth = 20;
sheets.sources.getRange("B:B").format.columnWidth = 66;
sheets.sources.getRange("C:C").format.columnWidth = 32;
sheets.sources.getRange("D:D").format.columnWidth = 16;
sheets.sources.getRange("E:E").format.columnWidth = 42;
sheets.sources.getRange("F:F").format.columnWidth = 18;
sheets.sources.getRange("G:G").format.columnWidth = 56;
sheets.sources.getRange("A4:G26").format.wrapText = true;

const noteRow = 20;
sheets.sources.getRange(`A${noteRow}:G${noteRow}`).merge();
sheets.sources.getRange(`A${noteRow}`).values = [["关键解释"]];
sheets.sources.getRange(`A${noteRow}:G${noteRow}`).format = { fill: COLORS.blue, font: { color: COLORS.white, bold: true } };
const notes = [
  ["1", "官方成交 priceSource=trade：历史上有人以该价成交，但不代表信号出现后你的订单一定能成交。"],
  ["2", "连续卖一 priceSource=best_ask：更接近可买报价，但目前完整样本只有 47 市场；不可把其样本量当作 351 市场。"],
  ["3", "净收益/份已按各运行 feeModel 计算；none 与 polymarket_crypto_taker 不能直接横比。"],
  ["4", "90–30 秒 size≥10 候选比 90/100/120–0 秒宽窗口多了成交量和时段过滤，因此后者不能用于否定或确认前者。"],
  ["5", "L4 盘口覆盖门槛下 0 个市场合格，当前深度矩阵只能说明数据覆盖不足，不能验证容量。"],
];
writeMatrix(sheets.sources, noteRow, 0, notes.map(([n, t]) => [n, t, null, null, null, null, null]));
sheets.sources.getRange(`A${noteRow + 1}:G${noteRow + notes.length}`).format = { fill: COLORS.gray, wrapText: true, borders: { preset: "all", style: "thin", color: COLORS.border } };
sheets.sources.getRange(`A${noteRow + 1}:A${noteRow + notes.length}`).format.font = { bold: true, color: COLORS.blue };
sheets.sources.getRange(`B${noteRow + 1}:G${noteRow + notes.length}`).merge(true);
sheets.sources.getRange(`A${noteRow + 1}:G${noteRow + notes.length}`).format.rowHeight = 30;

// 总览（公式引用明细表）
setTitle(sheets.overview, "BTC 5分钟涨跌市场｜历史拉取数据全部回测总览", "范围：所有已拉取数据回测（含 100/120 秒补测）；明确排除实时纸上交易。生成日期：2026-08-21（Asia/Shanghai）。", 12);
sheets.overview.getRange("A4:M4").merge();
sheets.overview.getRange("A4").values = [["覆盖规模"]];
sheets.overview.getRange("A4:M4").format = { fill: COLORS.blue, font: { color: COLORS.white, bold: true, size: 12 } };
const kpiLabels = [["历史运行数", "范围结果行", "价格分桶行", "压力矩阵行", "时间切片行", "盘口深度行", "失败样本行"]];
writeMatrix(sheets.overview, 5, 0, kpiLabels);
sheets.overview.getRange("A6").formulas = [[`=COUNTA('运行索引'!$A$5:$A$${4 + runValues.length})`]];
sheets.overview.getRange("B6").formulas = [[`=COUNTA('范围结果'!$A$5:$A$${4 + rangeResults.length})`]];
sheets.overview.getRange("C6").formulas = [[`=COUNTA('价格分桶'!$A$5:$A$${4 + bucketResults.length})`]];
sheets.overview.getRange("D6").formulas = [[`=COUNTA('压力测试'!$A$5:$A$${4 + pressureRows.length})`]];
sheets.overview.getRange("E6").formulas = [[`=COUNTA('时间切片'!$A$5:$A$${4 + timeRows.length})`]];
sheets.overview.getRange("F6").formulas = [[`=COUNTA('盘口深度'!$A$5:$A$${4 + depthRows.length})`]];
sheets.overview.getRange("G6").formulas = [[`=COUNTA('失败样本'!$A$5:$A$${4 + failureRows.length})`]];
sheets.overview.getRange("A5:G5").format = { fill: COLORS.teal, font: { color: COLORS.white, bold: true }, horizontalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.border } };
sheets.overview.getRange("A6:G6").format = { fill: COLORS.paleTeal, font: { color: COLORS.navy, bold: true, size: 15 }, horizontalAlignment: "center", borders: { preset: "all", style: "thin", color: COLORS.border }, numberFormat: "0" };
sheets.overview.getRange("A5:G6").format.columnWidth = 18;

sheets.overview.getRange("A8:M8").merge();
sheets.overview.getRange("A8").values = [["当前结论（把执行条件与宽窗口分开看）"]];
sheets.overview.getRange("A8:M8").format = { fill: COLORS.blue, font: { color: COLORS.white, bold: true, size: 12 } };
const conclusions = [
  ["首选历史候选", "DOWN 0.80–0.85｜90–30秒｜历史成交 size≥10", "combined351：57 笔，51 胜，89.47%，净收益 +0.0657/份。same47 仍为正，但只 8 笔。"],
  ["次选历史候选", "DOWN 0.75–0.80｜向下穿越｜90–30秒", "combined351：101 笔，83.17%，+0.0377/份；same47 为 -0.0830/份，跨源不稳。"],
  ["100秒补测", "0.80–0.85，含 size≥10 对照", "未过滤 ALL/UP/DOWN 均负；DOWN size≥10 为 -0.0017/份，接近盈亏平衡但没有优势。"],
  ["120秒补测", "0.80–0.85，含 size≥10 对照", "未过滤 ALL/UP/DOWN 均负；DOWN size≥10 为 +0.0199/份，但明显弱于 90–30秒的 +0.0657。"],
  ["UP方向", "目前没有跨样本、跨数据源稳定为正的候选", "UP 可继续实时记录，但历史结果不足以作为独立入场策略。"],
  ["盘口容量", "尚未验证", "L4 在 5 秒覆盖门槛下 0 个市场合格，不能据此判断真实可成交规模。"],
];
writeMatrix(sheets.overview, 8, 0, [["主题", "策略/窗口", "结论"], ...conclusions]);
formatHeader(sheets.overview.getRange("A9:C9"));
sheets.overview.getRange("A10:C15").format = { borders: { preset: "all", style: "thin", color: COLORS.border }, wrapText: true, verticalAlignment: "center" };
sheets.overview.getRange("A10:A15").format = { fill: COLORS.paleBlue, font: { color: COLORS.blue, bold: true }, borders: { preset: "all", style: "thin", color: COLORS.border }, wrapText: true };
sheets.overview.getRange("A:A").format.columnWidth = 20;
sheets.overview.getRange("B:B").format.columnWidth = 48;
sheets.overview.getRange("C:C").format.columnWidth = 78;
sheets.overview.getRange("A10:C15").format.rowHeight = 36;

const chartTitleRow = 17;
sheets.overview.getRange(`A${chartTitleRow}:M${chartTitleRow}`).merge();
sheets.overview.getRange(`A${chartTitleRow}`).values = [["90–30秒核心候选｜跨样本净收益/份"]];
sheets.overview.getRange(`A${chartTitleRow}:M${chartTitleRow}`).format = { fill: COLORS.blue, font: { color: COLORS.white, bold: true, size: 12 } };
sheets.overview.getRange("A18:B18").values = [["候选｜样本", "净收益/份"]];
formatHeader(sheets.overview.getRange("A18:B18"));
for (let i = 0; i < practicalRows.length; i += 1) {
  const sourceRow = coreStart + i;
  const targetRow = 19 + i;
  sheets.overview.getRange(`A${targetRow}`).formulas = [[`=IF('核心候选'!A${sourceRow}="down_80_85_trade_size_gte_10","80–85 size≥10","75–80 down-cross")&" | "&'核心候选'!B${sourceRow}`]];
  sheets.overview.getRange(`B${targetRow}`).formulas = [[`='核心候选'!I${sourceRow}`]];
}
sheets.overview.getRange(`A19:B${18 + practicalRows.length}`).format = { borders: { preset: "all", style: "thin", color: COLORS.border } };
sheets.overview.getRange(`B19:B${18 + practicalRows.length}`).format.numberFormat = "+0.0000;-0.0000;0.0000";
applyPnlConditional(sheets.overview, 1, 19, 18 + practicalRows.length);
const chart = sheets.overview.charts.add("bar", sheets.overview.getRange(`A18:B${18 + practicalRows.length}`));
chart.setPosition("E18", "M34");
chart.title = "90–30秒核心候选跨样本净收益/份";
chart.titleTextStyle.fontSize = 12;
chart.hasLegend = false;
chart.xAxis = { axisType: "textAxis", textStyle: { fontSize: 9 } };
chart.yAxis = { numberFormatCode: "+0.00;-0.00;0.00" };
sheets.overview.freezePanes.freezeRows(3);

// 检查
setTitle(sheets.checks, "工作簿完整性检查", "所有检查均以工作簿公式引用明细表；PASS 表示行数与原始文件清点一致。", 5);
const checkHeaders = ["检查项", "期望值", "公式结果", "状态", "说明", "来源"];
writeMatrix(sheets.checks, 3, 0, [checkHeaders]);
const checks = [
  ["历史运行数", reportPaths.length, null, null, "每个 last_window_report.json 一行", "运行索引"],
  ["范围结果行", rangeResults.length, null, null, "所有 bucketType=RANGE", "范围结果"],
  ["价格分桶行", bucketResults.length, null, null, "所有非 RANGE 输出", "价格分桶"],
  ["压力测试行", pressureRows.length, null, null, "主矩阵 + same47", "压力测试"],
  ["时间切片行", timeRows.length, null, null, "70/30 + 7 blocks", "时间切片"],
  ["盘口深度行", depthRows.length, null, null, "L4 原始 + coverage gated", "盘口深度"],
  ["核心候选行", practicalRows.length, null, null, "practical_candidates_90_30.csv", "核心候选"],
  ["100/120候选补测行", practicalWideRows.length, null, null, "practical_candidates_100_0/120_0.csv", "核心候选"],
  ["标准宽窗口行", supplementRows.length, null, null, "90/100/120秒 0.80–0.85 RANGE", "核心候选"],
  ["失败样本行", failureRows.length, null, null, "DOWN/UP 失败分析 JSON", "失败样本"],
];
writeMatrix(sheets.checks, 4, 0, checks);
const checkFormulas = [
  `=COUNTA('运行索引'!$A$5:$A$${4 + runValues.length})`,
  `=COUNTA('范围结果'!$A$5:$A$${4 + rangeResults.length})`,
  `=COUNTA('价格分桶'!$A$5:$A$${4 + bucketResults.length})`,
  `=COUNTA('压力测试'!$A$5:$A$${4 + pressureRows.length})`,
  `=COUNTA('时间切片'!$A$5:$A$${4 + timeRows.length})`,
  `=COUNTA('盘口深度'!$A$5:$A$${4 + depthRows.length})`,
  `=COUNTA('核心候选'!$A$${coreStart}:$A$${coreEnd})`,
  `=COUNTA('核心候选'!$A$${wideStart}:$A$${wideEnd})`,
  `=COUNTA('核心候选'!$A$${supStart}:$A$${supEnd})`,
  `=COUNTA('失败样本'!$A$5:$A$${4 + failureRows.length})`,
];
for (let i = 0; i < checks.length; i += 1) {
  const row = 5 + i;
  sheets.checks.getRange(`C${row}`).formulas = [[checkFormulas[i]]];
  sheets.checks.getRange(`D${row}`).formulas = [[`=IF(B${row}=C${row},"PASS","FAIL")`]];
}
formatDataBlock(sheets.checks, 4, checkHeaders, checks.length, { tableName: "ChecksTable", tableStyle: "TableStyleMedium4" });
sheets.checks.getRange(`D5:D${4 + checks.length}`).conditionalFormats.add("containsText", { text: "PASS", format: { fill: COLORS.paleGreen, font: { color: COLORS.green, bold: true } } });
sheets.checks.getRange(`D5:D${4 + checks.length}`).conditionalFormats.add("containsText", { text: "FAIL", format: { fill: COLORS.paleRed, font: { color: COLORS.red, bold: true } } });
sheets.checks.getRange("A:A").format.columnWidth = 26;
sheets.checks.getRange("B:D").format.columnWidth = 16;
sheets.checks.getRange("E:F").format.columnWidth = 42;

await fs.mkdir(outputDir, { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(wb);
await xlsx.save(outputPath);

const inspect = await wb.inspect({ kind: "workbook,sheet,table,drawing", maxChars: 9000, tableMaxRows: 3, tableMaxCols: 5 });
console.log(inspect.ndjson ?? inspect);
console.log(JSON.stringify({ outputPath, reportCount: reportPaths.length, rangeRows: rangeResults.length, bucketRows: bucketResults.length, pressureRows: pressureRows.length, timeRows: timeRows.length, depthRows: depthRows.length, practicalRows: practicalRows.length, practicalWideRows: practicalWideRows.length, supplementRows: supplementRows.length, failureRows: failureRows.length }, null, 2));
