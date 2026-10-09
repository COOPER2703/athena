#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from grpc_tools import protoc


def generate(out_dir: Path) -> None:
    proto_dir = Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)

    code = protoc.main(
        [
            "grpc_tools.protoc",
            f"--proto_path={proto_dir}",
            f"--python_out={out_dir}",
            str(proto_dir / "messages.proto"),
        ]
    )
    if code != 0:
        raise SystemExit(code)

    init = out_dir / "__init__.py"
    if not init.exists():
        init.write_text("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Python code from messages.proto")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "generated"),
        help="output directory (default: protocol/generated)",
    )
    args = parser.parse_args(argv)
    generate(Path(args.out).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
