"""Run multiple traffic notices sequentially through traffic_notice_test.py."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import time

from traffic_schema.review_export import write_review_queue


PROJECT_DIR = Path(__file__).resolve().parent
SINGLE_NOTICE_SCRIPT = PROJECT_DIR / "traffic_notice_test.py"


for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


@dataclass(frozen=True)
class RunResult:
    input_path: Path
    return_code: int
    attempts: int
    elapsed_seconds: float
    log_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按文件名顺序逐篇处理中文交通管制通告。"
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="要处理的文本文件；也可以改用 --input-dir。",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        help="从该目录读取文本文件。",
    )
    parser.add_argument(
        "--pattern",
        default="*.txt",
        help="配合 --input-dir 使用的文件匹配模式，默认 *.txt。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="所有结果和日志的输出目录，默认 output。",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="单篇失败后的重试次数，默认 1。",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="重试前等待秒数，默认 5。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只检查并显示执行计划，不启动 Ollama 推理。",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="可选 event_id 清单，原样传给单篇脚本。",
    )
    return parser.parse_args()


def collect_inputs(args: argparse.Namespace) -> list[Path]:
    if args.inputs and args.input_dir is not None:
        raise ValueError("不能同时使用位置参数 inputs 和 --input-dir。")
    if args.retries < 0:
        raise ValueError("--retries 不能小于 0。")
    if args.retry_delay < 0:
        raise ValueError("--retry-delay 不能小于 0。")

    if args.input_dir is not None:
        input_dir = args.input_dir.resolve()
        if not input_dir.is_dir():
            raise FileNotFoundError(f"找不到输入目录：{input_dir}")
        paths = sorted(
            path.resolve()
            for path in input_dir.glob(args.pattern)
            if path.is_file()
        )
    else:
        paths = [path.resolve() for path in args.inputs]

    if not paths:
        raise ValueError("没有找到待处理的文本文件。")

    missing = [path for path in paths if not path.is_file()]
    if missing:
        missing_text = "\n".join(f"  {path}" for path in missing)
        raise FileNotFoundError(f"以下输入文件不存在：\n{missing_text}")

    empty = [path for path in paths if path.stat().st_size == 0]
    if empty:
        empty_text = "\n".join(f"  {path}" for path in empty)
        raise ValueError(
            "以下输入文件为空，请先在编辑器中保存正文：\n" + empty_text
        )

    unique_paths = list(dict.fromkeys(paths))
    if len(unique_paths) != len(paths):
        raise ValueError("输入列表包含重复文件。")

    stems: dict[str, list[Path]] = {}
    for path in unique_paths:
        stems.setdefault(path.stem.casefold(), []).append(path)
    duplicate_stems = [group for group in stems.values() if len(group) > 1]
    if duplicate_stems:
        details = "\n".join(
            "  " + " | ".join(str(path) for path in group)
            for group in duplicate_stems
        )
        raise ValueError(
            "存在同名文件，输出前缀会冲突。请先重命名：\n" + details
        )

    return unique_paths


def run_notice(
    input_path: Path,
    output_dir: Path,
    retries: int,
    retry_delay: float,
    manifest: Path | None = None,
) -> RunResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{input_path.stem}_run.log"
    command = [
        sys.executable,
        str(SINGLE_NOTICE_SCRIPT),
        "--input",
        str(input_path),
        "--output-dir",
        str(output_dir),
    ]
    if manifest is not None:
        command.extend(["--manifest", str(manifest.resolve())])

    batch_started_at = datetime.now().astimezone()
    batch_started = time.monotonic()
    attempt_logs: list[str] = []
    completed: subprocess.CompletedProcess[str] | None = None
    attempt_count = 0

    for attempt_count in range(1, retries + 2):
        attempt_started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        attempt_elapsed = time.monotonic() - attempt_started
        attempt_logs.append(
            f"\n===== attempt {attempt_count} =====\n"
            f"elapsed_seconds: {attempt_elapsed:.2f}\n"
            f"return_code: {completed.returncode}\n"
            "\n----- stdout -----\n"
            f"{completed.stdout}"
            "\n----- stderr -----\n"
            f"{completed.stderr}"
        )
        if completed.returncode == 0:
            break
        if attempt_count <= retries:
            time.sleep(retry_delay)

    if completed is None:
        raise RuntimeError("未执行任何子进程")
    elapsed = time.monotonic() - batch_started

    log_content = (
        f"input: {input_path}\n"
        f"started_at: {batch_started_at.isoformat()}\n"
        f"attempts: {attempt_count}\n"
        f"elapsed_seconds: {elapsed:.2f}\n"
        f"return_code: {completed.returncode}\n"
        + "".join(attempt_logs)
    )
    log_path.write_text(log_content, encoding="utf-8")
    return RunResult(
        input_path=input_path,
        return_code=completed.returncode,
        attempts=attempt_count,
        elapsed_seconds=elapsed,
        log_path=log_path,
    )


def print_plan(input_paths: list[Path], output_dir: Path) -> None:
    print("执行计划：")
    print(f"  文件数量：{len(input_paths)}")
    print("  处理方式：串行（每次一篇）")
    print(f"  输出目录：{output_dir}")
    for index, path in enumerate(input_paths, start=1):
        print(f"  [{index}] {path}")


def merge_review_queues(input_paths: list[Path], output_dir: Path) -> Path:
    """Merge per-notice review files produced by successful subprocesses."""
    rows: list[dict[str, str]] = []
    for input_path in input_paths:
        review_path = output_dir / f"{input_path.stem}_review.csv"
        if not review_path.is_file():
            continue
        with review_path.open("r", encoding="utf-8-sig", newline="") as file_handle:
            rows.extend(csv.DictReader(file_handle))
    return write_review_queue(rows, output_dir / "review_queue.csv")


def main() -> int:
    args = parse_args()
    try:
        input_paths = collect_inputs(args)
        output_dir = args.output_dir.resolve()
        print_plan(input_paths, output_dir)
        if args.dry_run:
            print("\n检查完成：dry-run 未启动模型。")
            return 0

        print("\n开始按顺序处理……")
        results: list[RunResult] = []
        for index, input_path in enumerate(input_paths, start=1):
            print(f"[{index}/{len(input_paths)}] 正在处理 {input_path.name}……")
            try:
                result = run_notice(
                    input_path,
                    output_dir,
                    args.retries,
                    args.retry_delay,
                    args.manifest,
                )
            except Exception as exc:  # Record the exception and continue in order.
                print(f"[异常] {input_path.name}: {type(exc).__name__}: {exc}")
                continue
            results.append(result)
            status = "成功" if result.return_code == 0 else "失败"
            print(
                f"[{status}] {input_path.name} "
                f"({result.elapsed_seconds:.1f} 秒, "
                f"尝试 {result.attempts} 次) -> {result.log_path.name}"
            )

        successful = sum(result.return_code == 0 for result in results)
        failed_processes = sum(result.return_code != 0 for result in results)
        crashed = len(input_paths) - len(results)
        failed = failed_processes + crashed
        review_path = merge_review_queues(input_paths, output_dir)
        print("\n批量处理统计：")
        print(f"  总数：{len(input_paths)}")
        print(f"  成功：{successful}")
        print(f"  失败：{failed}")
        print(f"  日志目录：{output_dir}")
        print(f"  人工复核表：{review_path}")
        return 0 if failed == 0 else 1
    except Exception as exc:
        print(f"批量任务未启动：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
