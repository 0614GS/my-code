"""从锁文件构建 Harbor 可安装的不可变 my-code artifact 集。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from my_code.version import __version__


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    uv_source = shutil.which("uv")
    if uv_source is None:
        parser.error("uv executable not found")
    python_version = (root / ".python-version").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"3\.12(?:\.\d+)?", python_version) is None:
        parser.error(".python-version must select Python 3.12")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=root,
        check=True,
    )
    constraints = output / "constraints.txt"
    exported = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
        ],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    constraints.write_text(exported.stdout, encoding="utf-8")
    uv_binary = output / "uv"
    shutil.copy2(uv_source, uv_binary)
    wheels = sorted(output.glob("my_code-*.whl"))
    if len(wheels) != 1:
        parser.error(f"expected exactly one my-code wheel, found {len(wheels)}")
    wheel = wheels[0]
    manifest = {
        "schema_version": 1,
        "version": __version__,
        "wheel": wheel.name,
        "wheel_sha256": sha256(wheel),
        "constraints_sha256": sha256(constraints),
        "lock_sha256": sha256(root / "uv.lock"),
        "python_version": python_version,
        "requires_python": ">=3.12",
        "uv": uv_binary.name,
        "uv_sha256": sha256(uv_binary),
        "uv_version": subprocess.run(
            [uv_source, "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "harbor_version": "0.23.0",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
