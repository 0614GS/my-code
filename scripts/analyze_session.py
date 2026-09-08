"""离线查看 harness badcase；只读执行证据，无需模型或 Collector。"""

import argparse
import json
from pathlib import Path

from my_code.observability.diagnostic_log import read_diagnostic_timeline
from my_code.sessions.diagnostics import build_diagnostic_report, request_evidence
from my_code.sessions.inspection import inspect_session


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_state_dir", type=Path)
    parser.add_argument("session_id")
    parser.add_argument("--include-content", action="store_true")
    parser.add_argument("--request-id", help="显式输出单个请求的完整语义输入")
    args = parser.parse_args()
    try:
        snapshot = inspect_session(args.project_state_dir, args.session_id)
        report = (
            request_evidence(snapshot, args.request_id)
            if args.request_id
            else build_diagnostic_report(snapshot, include_content=args.include_content)
        )
        report["diagnostic_timeline"] = read_diagnostic_timeline(
            args.project_state_dir,
            args.session_id,
            request_id=args.request_id,
        )
    except (OSError, ValueError) as error:
        parser.exit(2, f"Cannot inspect session: {type(error).__name__}\n")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
