"""联合 Harbor trial 与 my-code 原生证据生成确定性评测报告。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

_ANALYZER_PATH = Path(__file__).parents[1] / "integrations" / "harbor" / "analyzer.py"
_SPEC = importlib.util.spec_from_file_location("mycode_harbor_analyzer", _ANALYZER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Cannot load Harbor analyzer")
_ANALYZER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _ANALYZER
_SPEC.loader.exec_module(_ANALYZER)
analyze_harbor_job = _ANALYZER.analyze_harbor_job
render_summary = _ANALYZER.render_summary
trials_csv = _ANALYZER.trials_csv
write_report = _ANALYZER.write_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        help="兼容模式：仅向 stdout 输出完整 JSON 或 trial CSV，不写报告目录",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="报告目录；默认是 <job-dir>/analysis",
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="包含工具参数和结果正文；可能含敏感内容",
    )
    args = parser.parse_args()
    try:
        report = analyze_harbor_job(args.job_dir, include_content=args.include_content)
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        if args.format == "csv":
            print(trials_csv(report), end="")
            return
        output_dir = args.output_dir or args.job_dir / "analysis"
        paths = write_report(report, output_dir)
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(2, f"Cannot analyze Harbor job: {type(error).__name__}: {error}\n")
    print(render_summary(report, paths))


if __name__ == "__main__":
    main()
