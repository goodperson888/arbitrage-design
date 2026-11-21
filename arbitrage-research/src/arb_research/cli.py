from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from .backtest import run_backtest
from .io import load_json, read_events, write_events, write_fills, write_json
from .strategy import LastWindowThresholdConfig, LastWindowThresholdStrategy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb-research")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backtest = subparsers.add_parser("backtest", help="Run a prediction-market backtest.")
    backtest.add_argument("--events", required=True, type=Path, help="Standard MarketEvent NDJSON file.")
    backtest.add_argument("--config", required=True, type=Path, help="Strategy JSON config.")
    backtest.add_argument("--out", required=True, type=Path, help="Output run directory.")

    backfill = subparsers.add_parser("backfill", help="Backfill historical data from platforms.")
    backfill.add_argument("--venue", required=True, choices=["polymarket", "binance"], help="Platform to backfill from.")
    backfill.add_argument("--symbol", type=str, help="Symbol for Binance (e.g., BTCUSDT).")
    backfill.add_argument("--limit", type=int, default=100, help="Number of items to fetch.")
    backfill.add_argument("--proxy", type=str, help="Proxy URL (e.g., http://127.0.0.1:7890).")
    backfill.add_argument("--out", required=True, type=Path, help="Output NDJSON file.")

    collect = subparsers.add_parser("collect", help="Collect real-time data from platforms.")
    collect.add_argument("--venue", required=True, choices=["binance"], help="Platform to collect from.")
    collect.add_argument("--symbols", required=True, help="Comma-separated symbols (e.g., BTCUSDT,ETHUSDT).")
    collect.add_argument("--output-dir", required=True, type=Path, help="Output directory for recordings.")
    collect.add_argument("--proxy", type=str, help="Proxy URL (e.g., http://127.0.0.1:7890).")
    collect.add_argument("--max-file-size-mb", type=int, default=128, help="Max file size before rotation.")
    collect.add_argument("--queue-size", type=int, default=10000, help="Event queue size.")

    import_arbdesk = subparsers.add_parser("import-arbdesk", help="Import ArbDesk-recorded NDJSON files.")
    import_arbdesk.add_argument("--input-dir", required=True, type=Path, help="ArbDesk recording directory.")
    import_arbdesk.add_argument("--out", required=True, type=Path, help="Output consolidated NDJSON file.")
    import_arbdesk.add_argument("--validate", action="store_true", help="Validate files before import.")

    convert = subparsers.add_parser("convert", help="Convert NDJSON files to Parquet format.")
    convert.add_argument("--input", required=True, type=Path, help="Input NDJSON file.")
    convert.add_argument("--output-dir", required=True, type=Path, help="Output directory for Parquet files.")
    convert.add_argument("--compression", type=str, default="snappy", choices=["snappy", "gzip", "brotli", "lz4"], help="Compression algorithm.")
    convert.add_argument("--row-group-size", type=int, default=10000, help="Row group size for Parquet.")
    convert.add_argument("--partition-by", type=str, help="Comma-separated columns to partition by (e.g., venue,eventType).")

    query = subparsers.add_parser("query", help="Query data using DuckDB SQL.")
    query.add_argument("--data-source", required=True, type=Path, help="Path to Parquet directory or NDJSON file.")
    query.add_argument("--database", type=Path, help="Path to DuckDB database file (default: in-memory).")
    query.add_argument("--sql", type=str, help="SQL query to execute.")
    query.add_argument("--summary", action="store_true", help="Show data summary instead of custom query.")
    query.add_argument("--market-id", type=str, help="Analyze specific market.")
    query.add_argument("--export-csv", type=Path, help="Export query results to CSV file.")

    quality = subparsers.add_parser("quality", help="Assess data quality level.")
    quality.add_argument("--input", required=True, type=Path, help="Input NDJSON file to analyze.")
    quality.add_argument("--detailed", action="store_true", help="Show detailed quality assessment.")

    validate_window = subparsers.add_parser(
        "validate-last-window",
        help="Validate final-window 0.70-0.88 prediction-market entries.",
    )
    validate_window.add_argument("--events", required=True, type=Path, help="MarketEvent NDJSON file.")
    validate_window.add_argument("--out", required=True, type=Path, help="Output report directory.")
    validate_window.add_argument("--windows", default="120,180", help="Comma-separated windows in seconds.")
    validate_window.add_argument("--price-min", type=Decimal, default=Decimal("0.70"))
    validate_window.add_argument("--price-max", type=Decimal, default=Decimal("0.88"))
    validate_window.add_argument("--bucket-size", type=Decimal, default=Decimal("0.01"))
    validate_window.add_argument("--order-size", type=Decimal, default=Decimal("1"))
    validate_window.add_argument("--min-ask-size", type=Decimal, default=Decimal("1"))
    validate_window.add_argument(
        "--min-remaining-seconds",
        type=int,
        default=0,
        help="Stop opening entries after remaining time falls below this value.",
    )
    validate_window.add_argument(
        "--fee-model",
        choices=["none", "polymarket_crypto_taker"],
        default="none",
    )
    validate_window.add_argument("--fee-rate", type=Decimal, default=Decimal("0"))
    validate_window.add_argument(
        "--price-source",
        choices=["best_ask", "trade"],
        default="best_ask",
        help="Use continuous best asks (L3) or observed taker BUY trades (L2).",
    )
    validate_window.add_argument(
        "--max-quote-gap-seconds",
        type=Decimal,
        default=Decimal("5"),
        help="Maximum allowed data gap inside each final window.",
    )
    validate_window.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Return success even when coverage is incomplete; report remains provisional.",
    )

    experiment = subparsers.add_parser("experiment", help="Manage and run backtesting experiments.")
    experiment_subparsers = experiment.add_subparsers(dest="experiment_command", required=True)

    # Create experiment
    create_exp = experiment_subparsers.add_parser("create", help="Create a new experiment.")
    create_exp.add_argument("--name", required=True, type=str, help="Experiment name.")
    create_exp.add_argument("--description", required=True, type=str, help="Experiment description.")
    create_exp.add_argument("--events", required=True, type=Path, help="Events file for backtesting.")
    create_exp.add_argument("--experiments-dir", type=Path, default=Path("experiments"), help="Base experiments directory.")
    create_exp.add_argument("--test-params", action="store_true", help="Use reduced parameter space for testing.")

    # Run experiment
    run_exp = experiment_subparsers.add_parser("run", help="Run an experiment.")
    run_exp.add_argument("--experiment-id", required=True, type=str, help="Experiment ID to run.")
    run_exp.add_argument("--experiments-dir", type=Path, default=Path("experiments"), help="Base experiments directory.")

    # List experiments
    list_exp = experiment_subparsers.add_parser("list", help="List all experiments.")
    list_exp.add_argument("--experiments-dir", type=Path, default=Path("experiments"), help="Base experiments directory.")
    list_exp.add_argument("--status", type=str, help="Filter by status (created, completed, archived).")

    # Show experiment results
    show_exp = experiment_subparsers.add_parser("show", help="Show experiment results.")
    show_exp.add_argument("--experiment-id", required=True, type=str, help="Experiment ID to show.")
    show_exp.add_argument("--experiments-dir", type=Path, default=Path("experiments"), help="Base experiments directory.")
    show_exp.add_argument("--top", type=int, default=10, help="Number of top performers to show.")

    args = parser.parse_args(argv)

    if args.command == "backtest":
        return _run_backtest(args.events, args.config, args.out)
    
    if args.command == "backfill":
        return _run_backfill(args.venue, args.symbol, args.limit, args.proxy, args.out)
    
    if args.command == "collect":
        return _run_collect(args.venue, args.symbols, args.output_dir, args.proxy, args.max_file_size_mb, args.queue_size)
    
    if args.command == "import-arbdesk":
        return _run_import_arbdesk(args.input_dir, args.out, args.validate)
    
    if args.command == "convert":
        partition_cols = args.partition_by.split(",") if args.partition_by else None
        return _run_convert(args.input, args.output_dir, args.compression, args.row_group_size, partition_cols)
    
    if args.command == "query":
        return _run_query(args.data_source, args.database, args.sql, args.summary, args.market_id, args.export_csv)
    
    if args.command == "quality":
        return _run_quality(args.input, args.detailed)

    if args.command == "validate-last-window":
        return _run_last_window_validation(args)
    
    if args.command == "experiment":
        return _run_experiment(args)

    parser.error(f"unknown command: {args.command}")
    return 2


def _run_backtest(events_path: Path, config_path: Path, out_dir: Path) -> int:
    config = LastWindowThresholdConfig.from_dict(load_json(config_path))
    strategy = LastWindowThresholdStrategy(config)
    result = run_backtest(read_events(events_path), strategy)

    write_fills(out_dir / "fills.ndjson", result.fills)
    write_json(out_dir / "metrics.json", result.metrics)

    print(f"fills: {result.metrics['fillCount']}")
    print(f"settled: {result.metrics['settledFillCount']}")
    print(f"win_rate: {result.metrics['winRate']}")
    print(f"net_pnl_per_share: {result.metrics['netPnlPerShare']}")
    print(f"wrote: {out_dir}")
    return 0


def _run_backfill(venue: str, symbol: str | None, limit: int, proxy: str | None, out_path: Path) -> int:
    """Run historical data backfill from a platform."""
    events = []
    
    if venue == "polymarket":
        from .connectors import PolymarketBackfill
        
        with PolymarketBackfill(proxy=proxy) as backfill:
            print(f"Fetching {limit} markets from Polymarket...")
            markets = backfill.get_markets(limit=limit, closed=True)
            print(f"Found {len(markets)} markets")
            
            for market in markets:
                print(f"Processing market: {market.question[:50]}...")
                market_events = backfill.market_to_events(market)
                events.extend(market_events)
                
                # Get trades for this market
                try:
                    trades = backfill.get_market_trades(market.market_id, limit=limit)
                    trade_events = backfill.trades_to_events(trades)
                    events.extend(trade_events)
                    print(f"  Added {len(trade_events)} trade events")
                except Exception as e:
                    print(f"  Warning: Could not fetch trades for {market.market_id}: {e}")
    
    elif venue == "binance":
        if not symbol:
            print("Error: --symbol is required for Binance backfill")
            return 1
        
        from .connectors import BinanceBackfill
        
        with BinanceBackfill(proxy=proxy) as backfill:
            print(f"Fetching {limit} klines for {symbol} from Binance...")
            klines = backfill.get_klines(symbol, limit=limit)
            print(f"Found {len(klines)} klines")
            
            events = backfill.klines_to_events(klines)
            
            print(f"Fetching {limit} trades for {symbol} from Binance...")
            trades = backfill.get_trades(symbol, limit=limit)
            print(f"Found {len(trades)} trades")
            
            trade_events = backfill.trades_to_events(trades)
            events.extend(trade_events)
    
    else:
        print(f"Error: Unsupported venue: {venue}")
        return 1
    
    # Sort events by source time
    events.sort(key=lambda e: e.source_time_us)
    
    # Write to NDJSON
    write_events(out_path, events)
    print(f"Wrote {len(events)} events to {out_path}")
    return 0


def _run_collect(
    venue: str,
    symbols: str,
    output_dir: Path,
    proxy: str | None,
    max_file_size_mb: int,
    queue_size: int,
) -> int:
    """Run real-time data collection from a platform."""
    symbol_list = [s.strip() for s in symbols.split(",")]
    
    if venue == "binance":
        from .recorders import BinanceRecorder, RecorderConfig
        
        config = RecorderConfig(
            base_dir=output_dir,
            venue=venue,
            max_file_size_mb=max_file_size_mb,
            queue_size=queue_size,
        )
        
        recorder = BinanceRecorder(config, symbol_list, proxy=proxy)
        print(f"Starting Binance recorder for symbols: {symbol_list}")
        print(f"Output directory: {output_dir}")
        print("Press Ctrl+C to stop...")
        
        try:
            recorder.collect()
        except KeyboardInterrupt:
            print("\nStopping recorder...")
            recorder.stop()
            return 0
        except Exception as e:
            print(f"Error during collection: {e}")
            return 1
    
    else:
        print(f"Error: Unsupported venue for collection: {venue}")
        return 1


def _run_import_arbdesk(input_dir: Path, out_path: Path, validate: bool) -> int:
    """Import ArbDesk-recorded NDJSON files."""
    from .importers import ArbDeskImporter
    
    importer = ArbDeskImporter(input_dir)
    
    # Get summary first
    print("Analyzing ArbDesk recording directory...")
    summary = importer.get_import_summary()
    
    print(f"Recording directory: {summary['recording_dir']}")
    print(f"Total files: {summary['total_files']}")
    print(f"Valid files: {summary['valid_files']}")
    print(f"Invalid files: {summary['invalid_files']}")
    print(f"Venues: {', '.join(summary['venues'])}")
    print(f"Dates: {', '.join(summary['dates'])}")
    print(f"Total events: {summary['total_events']}")
    
    if validate:
        print("\nValidating files...")
        files = importer.discover_files()
        for file_path in files:
            structure_validation = importer.validate_file_structure(file_path)
            event_validation = importer.validate_events(file_path)
            
            print(f"\n{file_path.name}:")
            print(f"  Structure: {'OK' if structure_validation['valid'] else 'FAIL'}")
            if not structure_validation['valid']:
                print(f"  Issues: {structure_validation['issues']}")
            
            print(f"  Events: {'OK' if event_validation['valid'] else 'FAIL'}")
            if not event_validation['valid']:
                print(f"  Issues: {event_validation['issues']}")
            else:
                print(f"  Event count: {event_validation['total_count']}")
    
    # Import events
    print(f"\nImporting events to {out_path}...")
    events = list(importer.import_directory())
    
    # Sort by source time
    events.sort(key=lambda e: e.source_time_us)
    
    # Write to output file
    write_events(out_path, events)
    
    print(f"Imported {len(events)} events to {out_path}")
    return 0


def _run_convert(
    input_file: Path,
    output_dir: Path,
    compression: str,
    row_group_size: int,
    partition_cols: list[str] | None,
) -> int:
    """Convert NDJSON file to Parquet format."""
    from .converters import NDJSONToParquetConverter, ConversionConfig
    
    config = ConversionConfig(
        input_file=input_file,
        output_dir=output_dir,
        row_group_size=row_group_size,
        compression=compression,
        partition_by=partition_cols,
    )
    
    converter = NDJSONToParquetConverter(config)
    
    # Get conversion stats first
    print("Analyzing input file...")
    stats = converter.get_conversion_stats()
    
    print(f"Event count: {stats['event_count']}")
    print(f"Input size: {stats['input_size_mb']:.2f} MB")
    print(f"Event types: {stats['event_types']}")
    print(f"Venues: {stats['venues']}")
    
    if stats['event_count'] == 0:
        print("No events to convert")
        return 1
    
    # Perform conversion
    print(f"\nConverting to Parquet with {compression} compression...")
    output_path = converter.convert()
    
    # Calculate output size
    if output_path.is_dir():
        # Partitioned dataset
        output_size = sum(f.stat().st_size for f in output_path.rglob("*") if f.is_file())
        print(f"Created partitioned dataset in {output_path}")
    else:
        # Single file
        output_size = output_path.stat().st_size if output_path.exists() else 0
        print(f"Created Parquet file: {output_path}")
    
    compression_ratio = (1 - output_size / (stats['input_size_mb'] * 1024 * 1024)) * 100 if stats['input_size_mb'] > 0 else 0
    
    print(f"Output size: {output_size / (1024 * 1024):.2f} MB")
    print(f"Compression ratio: {compression_ratio:.1f}%")
    
    return 0


def _run_query(
    data_source: Path,
    database: Path | None,
    sql: str | None,
    summary: bool,
    market_id: str | None,
    export_csv: Path | None,
) -> int:
    """Run DuckDB analytics on data."""
    from .analytics import DuckDBAnalytics, QueryConfig
    
    # Determine data source type
    is_parquet = data_source.is_dir() or data_source.suffix == ".parquet"
    is_ndjson = data_source.suffix == ".ndjson"
    
    if not (is_parquet or is_ndjson):
        print(f"Error: Unsupported data source: {data_source}")
        return 1
    
    config = QueryConfig(
        database_path=database,
        parquet_dir=data_source if is_parquet else None,
        ndjson_file=data_source if is_ndjson else None,
    )
    
    with DuckDBAnalytics(config) as analytics:
        # Show table info
        table_info = analytics.get_table_info()
        print(f"Data table info: {table_info}")
        
        if summary:
            # Show data summary
            print("\n=== Data Summary ===")
            event_summary = analytics.get_event_summary()
            print(f"Total events: {event_summary.get('total_events', 0)}")
            print(f"By event type: {event_summary.get('by_event_type', {})}")
            print(f"By venue: {event_summary.get('by_venue', {})}")
            print(f"Time range: {event_summary.get('time_range', {})}")
            print(f"Data quality: {event_summary.get('data_quality', {})}")
            
            if market_id:
                print(f"\n=== Market Summary: {market_id} ===")
                market_summary = analytics.get_market_summary(market_id)
                print(market_summary)
                
                print(f"\n=== Price Analysis ===")
                price_analysis = analytics.analyze_price_quotes(market_id)
                print(price_analysis)
        
        elif sql:
            # Execute custom query
            print(f"\n=== Query Results ===")
            results = analytics.query(sql)
            
            if results:
                # Print results as table
                for row in results:
                    print("\t".join(str(cell) for cell in row))
                
                # Export to CSV if requested
                if export_csv:
                    analytics.export_query_to_csv(sql, export_csv)
            else:
                print("No results returned")
        
        else:
            print("Error: Either --summary or --sql must be specified")
            return 1
    
    return 0


def _run_quality(input_file: Path, detailed: bool) -> int:
    """Run data quality assessment on a file."""
    from .quality import DataQualityGrader
    
    if not input_file.exists():
        print(f"Error: File not found: {input_file}")
        return 1
    
    print(f"Analyzing data quality for: {input_file}")
    
    grader = DataQualityGrader()
    assessment = grader.analyze_file(input_file)
    
    print(f"\n=== Data Quality Assessment ===")
    print(f"Quality Level: {assessment.level.value}")
    print(f"Quality Score: {assessment.score:.2f}")
    
    if detailed:
        print(f"\n--- Capabilities ---")
        for capability in assessment.capabilities:
            print(f"  ✓ {capability}")
        
        print(f"\n--- Limitations ---")
        for limitation in assessment.limitations:
            print(f"  ✗ {limitation}")
        
        print(f"\n--- Can Answer ---")
        for question in assessment.can_answer:
            print(f"  • {question}")
        
        print(f"\n--- Cannot Answer ---")
        for question in assessment.cannot_answer:
            print(f"  • {question}")
        
        # Get detailed report
        report = grader.get_quality_report()
        print(f"\n--- Detailed Report ---")
        print(f"Event types seen: {report['event_types_seen']}")
        print(f"Fields seen: {report['fields_seen']}")
        print(f"Event count: {report['event_count']}")
        print(f"Market count: {report['market_count']}")
        print(f"Time range: {report['time_range_us']}")
    
    return 0


def _run_last_window_validation(args: argparse.Namespace) -> int:
    """Generate the final-window direction and price-bucket validation report."""
    from .validation import build_last_window_report, write_last_window_report

    try:
        windows = tuple(sorted({int(item.strip()) for item in args.windows.split(",") if item.strip()}))
    except ValueError:
        print("Error: --windows must be a comma-separated list of integer seconds")
        return 2

    report = build_last_window_report(
        read_events(args.events),
        windows_seconds=windows,
        price_min=args.price_min,
        price_max=args.price_max,
        bucket_size=args.bucket_size,
        order_size=args.order_size,
        min_ask_size=args.min_ask_size,
        fee_model=args.fee_model,
        fee_rate=args.fee_rate,
        max_quote_gap_seconds=args.max_quote_gap_seconds,
        price_source=args.price_source,
        min_remaining_seconds=args.min_remaining_seconds,
    )
    json_path, csv_path = write_last_window_report(args.out, report)

    print("=== Last-window validation ===")
    print(f"defined markets: {report['dataset']['definedMarketCount']}")
    print(f"settled markets: {report['dataset']['settledMarketCount']}")
    print(f"dataset complete: {report['datasetComplete']}")
    print(f"analysis complete: {report['analysisComplete']}")
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")

    range_rows = [
        row for row in report["results"]
        if row["bucketType"] == "RANGE"
    ]
    print("\nwindow  outcome  trades  wins  win_rate  ci95  avg_entry  net_pnl/share")
    for row in range_rows:
        ci = f"{row['winRateCi95Low']}..{row['winRateCi95High']}"
        print(
            f"{row['windowSeconds']:>6}  {row['outcome']:<7}  "
            f"{row['settledTradeCount']:>6}  {row['winCount']:>4}  "
            f"{row['winRate']:>8}  {ci:<19}  "
            f"{row['averageEntryPrice']:>9}  {row['netPnlPerShare']:>13}"
        )

    print(f"\nJSON: {json_path}")
    print(f"CSV:  {csv_path}")
    if report["provisional"] and not args.allow_incomplete:
        print("Validation failed coverage checks; use --allow-incomplete only for provisional analysis.")
        return 2
    return 0


def _run_experiment(args) -> int:
    """Run experiment commands."""
    from .experiments import ExperimentManager
    
    manager = ExperimentManager(args.experiments_dir)
    
    if args.experiment_command == "create":
        # Create new experiment
        config = manager.create_experiment(
            experiment_name=args.name,
            description=args.description,
            strategy_type="prediction_last_window_threshold",
            events_file=args.events,
            metadata={"created_by": "cli"},
            use_test_params=args.test_params,
        )
        
        print(f"Created experiment: {config.experiment_name}")
        print(f"Experiment ID: {config.experiment_id}")
        print(f"Total parameter combinations: {config.parameter_space.total_combinations()}")
        print(f"Config saved to: {config.get_experiment_dir()}")
        print(f"\nTo run this experiment:")
        print(f"  python -m arb_research experiment run --experiment-id {config.experiment_id}")
        
        return 0
    
    elif args.experiment_command == "run":
        # Run experiment
        print(f"Running experiment: {args.experiment_id}")
        
        def progress_callback(current, total, params):
            print(f"Progress: {current}/{total} ({current/total*100:.1f}%)")
        
        result = manager.run_experiment(args.experiment_id, progress_callback)
        
        print(f"\nExperiment completed!")
        print(f"Total runs: {result.total_runs}")
        print(f"Successful: {result.successful_runs}")
        print(f"Failed: {result.failed_runs}")
        
        if result.summary_statistics:
            print(f"\nSummary Statistics:")
            print(f"  Average win rate: {result.summary_statistics.get('avg_win_rate', 'N/A')}")
            print(f"  Max win rate: {result.summary_statistics.get('max_win_rate', 'N/A')}")
            print(f"  Average net PnL per share: {result.summary_statistics.get('avg_net_pnl_per_share', 'N/A')}")
            print(f"  Max net PnL per share: {result.summary_statistics.get('max_net_pnl_per_share', 'N/A')}")
        
        return 0
    
    elif args.experiment_command == "list":
        # List experiments
        experiments = manager.list_experiments(status=args.status)
        
        if not experiments:
            print("No experiments found.")
            return 0
        
        print(f"Found {len(experiments)} experiment(s):")
        print(f"{'ID':<12} {'Name':<30} {'Status':<12} {'Created'}")
        print("-" * 70)
        
        for exp in experiments:
            exp_id_short = exp["experiment_id"][:8]
            print(f"{exp_id_short:<12} {exp['experiment_name']:<30} {exp['status']:<12} {exp['created_at'][:19]}")
        
        return 0
    
    elif args.experiment_command == "show":
        # Show experiment results
        from .experiments import ParameterSweep
        
        config = manager.get_experiment_config(args.experiment_id)
        result = manager.get_experiment_result(args.experiment_id)
        
        if config is None:
            print(f"Experiment not found: {args.experiment_id}")
            return 1
        
        print(f"Experiment: {config.experiment_name}")
        print(f"ID: {config.experiment_id}")
        print(f"Description: {config.description}")
        print(f"Status: {result.summary_statistics.get('status', 'unknown') if result else 'no results'}")
        
        if result:
            print(f"\nResults Summary:")
            print(f"  Total runs: {result.total_runs}")
            print(f"  Successful: {result.successful_runs}")
            print(f"  Failed: {result.failed_runs}")
            
            if result.summary_statistics:
                print(f"\nPerformance:")
                print(f"  Average win rate: {result.summary_statistics.get('avg_win_rate', 'N/A')}")
                print(f"  Max win rate: {result.summary_statistics.get('max_win_rate', 'N/A')}")
                print(f"  Average net PnL per share: {result.summary_statistics.get('avg_net_pnl_per_share', 'N/A')}")
                print(f"  Max net PnL per share: {result.summary_statistics.get('max_net_pnl_per_share', 'N/A')}")
            
            # Create temporary sweep to get top performers
            from .experiments import ParameterSweep
            sweep = ParameterSweep(config)
            sweep.result = result
            
            top_performers = sweep.get_top_performers(n=args.top)
            
            print(f"\nTop {args.top} Performers:")
            print(f"{'Rank':<6} {'Window':<8} {'Threshold':<10} {'Max Entry':<10} {'Win Rate':<10} {'Net PnL':<12}")
            print("-" * 70)
            
            for i, run in enumerate(top_performers, 1):
                print(f"{i:<6} {run.parameters.get('window_seconds'):<8} "
                      f"{str(run.parameters.get('threshold_price')):<10} "
                      f"{str(run.parameters.get('max_entry_price')):<10} "
                      f"{run.metrics.get('winRate', 'N/A'):<10} "
                      f"{run.metrics.get('netPnlPerShare', 'N/A'):<12}")
        
        return 0
    
    else:
        print(f"Unknown experiment command: {args.experiment_command}")
        return 1
