#!/usr/bin/env python3
import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_OLD_COMMIT = "5adf2c52307a33dbbcbd25d2047ba2fa4a5698b9"
DEFAULT_NEW_COMMIT = "0bf964975bf3216e59f1a5e8f3af77169480b66d"
DEFAULT_ROWS = 50_000_000
BENCHMARK_PATTERN = "benchmark/micro/zonemaps/append_numeric_stats_.*"

BENCHMARKS = [
    ("BIGINT", "benchmark/micro/zonemaps/append_numeric_stats_bigint.benchmark", 8),
    ("DECIMAL(18,6)", "benchmark/micro/zonemaps/append_numeric_stats_decimal_18_6.benchmark", 8),
    ("DECIMAL(38,10)", "benchmark/micro/zonemaps/append_numeric_stats_decimal_38_10.benchmark", 16),
    ("DECIMAL(4,1)", "benchmark/micro/zonemaps/append_numeric_stats_decimal_4_1.benchmark", 2),
    ("DECIMAL(9,4)", "benchmark/micro/zonemaps/append_numeric_stats_decimal_9_4.benchmark", 4),
    ("DOUBLE", "benchmark/micro/zonemaps/append_numeric_stats_double.benchmark", 8),
    ("FLOAT", "benchmark/micro/zonemaps/append_numeric_stats_float.benchmark", 4),
    ("HUGEINT", "benchmark/micro/zonemaps/append_numeric_stats_hugeint.benchmark", 16),
    ("INTEGER", "benchmark/micro/zonemaps/append_numeric_stats_integer.benchmark", 4),
    ("SMALLINT", "benchmark/micro/zonemaps/append_numeric_stats_smallint.benchmark", 2),
    ("TINYINT", "benchmark/micro/zonemaps/append_numeric_stats_tinyint.benchmark", 1),
    ("UBIGINT", "benchmark/micro/zonemaps/append_numeric_stats_ubigint.benchmark", 8),
    ("UHUGEINT", "benchmark/micro/zonemaps/append_numeric_stats_uhugeint.benchmark", 16),
    ("UINTEGER", "benchmark/micro/zonemaps/append_numeric_stats_uinteger.benchmark", 4),
    ("USMALLINT", "benchmark/micro/zonemaps/append_numeric_stats_usmallint.benchmark", 2),
    ("UTINYINT", "benchmark/micro/zonemaps/append_numeric_stats_utinyint.benchmark", 1),
]

BENCHMARK_BY_PATH = {
    benchmark_path: {
        "type": type_name,
        "benchmark": benchmark_path,
        "bytes_per_value": bytes_per_value,
    }
    for type_name, benchmark_path, bytes_per_value in BENCHMARKS
}


def run_command(args, cwd, timeout=None, env=None, log_path=None):
    start = time.time()
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)
    elapsed = time.time() - start
    output = proc.stdout + proc.stderr
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as f:
            f.write("$ ")
            f.write(" ".join(str(arg) for arg in args))
            f.write("\n")
            f.write("cwd: ")
            f.write(str(cwd))
            f.write("\n")
            f.write("exit_code: ")
            f.write(str(proc.returncode))
            f.write("\n")
            f.write("elapsed_s: %.3f\n\n" % elapsed)
            f.write(output)
    if proc.returncode != 0:
        detail = output[-4000:] if output else ""
        raise RuntimeError("command failed: %s\n%s" % (" ".join(str(arg) for arg in args), detail))
    return output


def probe_command(args):
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def linux_memtotal_bytes():
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    with open(meminfo) as f:
        for line in f:
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2:
                    value = parse_int(parts[1])
                    if value is not None:
                        return value * 1024
    return None


def linux_cpuinfo_values(key):
    cpuinfo = Path("/proc/cpuinfo")
    values = []
    if not cpuinfo.exists():
        return values
    with open(cpuinfo) as f:
        for line in f:
            if ":" not in line:
                continue
            line_key, value = line.split(":", 1)
            if line_key.strip() == key:
                values.append(value.strip())
    return values


def linux_cpuinfo_blocks():
    cpuinfo = Path("/proc/cpuinfo")
    if not cpuinfo.exists():
        return []
    blocks = []
    current = {}
    with open(cpuinfo) as f:
        for line in f:
            line = line.strip()
            if not line:
                if current:
                    blocks.append(current)
                    current = {}
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            current[key.strip()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


def total_memory_bytes():
    if platform.system() == "Linux":
        value = linux_memtotal_bytes()
        if value is not None:
            return value
    if platform.system() == "Darwin":
        value = parse_int(probe_command(["sysctl", "-n", "hw.memsize"]))
        if value is not None:
            return value
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return pages * page_size
        except (OSError, ValueError):
            return None
    return None


def cpu_model():
    if platform.system() == "Linux":
        for key in ("model name", "Hardware", "Processor"):
            values = linux_cpuinfo_values(key)
            if values:
                return values[0]
    if platform.system() == "Darwin":
        value = probe_command(["sysctl", "-n", "machdep.cpu.brand_string"])
        if value:
            return value
    value = platform.processor()
    return value or None


def physical_cpu_cores():
    if platform.system() == "Linux":
        core_ids = set()
        for block in linux_cpuinfo_blocks():
            physical_id = block.get("physical id")
            core_id = block.get("core id")
            if physical_id is None or core_id is None:
                continue
            core_ids.add((physical_id, core_id))
        if core_ids:
            return len(core_ids)
    if platform.system() == "Darwin":
        value = parse_int(probe_command(["sysctl", "-n", "hw.physicalcpu"]))
        if value is not None:
            return value
    return None


def format_gib(byte_count):
    if byte_count is None:
        return ""
    return "%.1f GiB" % (byte_count / 1024.0 / 1024.0 / 1024.0)


def collect_system_metadata():
    memory_bytes = total_memory_bytes()
    return {
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "cpu": {
            "model": cpu_model(),
            "physical_cores": physical_cpu_cores(),
            "logical_cores": os.cpu_count(),
        },
        "memory": {
            "total_bytes": memory_bytes,
            "total_gib": format_gib(memory_bytes),
        },
        "python": sys.version.split()[0],
    }


def append_system_summary(lines, system_metadata):
    os_metadata = system_metadata["os"]
    cpu_metadata = system_metadata["cpu"]
    memory_metadata = system_metadata["memory"]
    cpu_cores = "%s logical" % cpu_metadata["logical_cores"]
    if cpu_metadata["physical_cores"] is not None:
        cpu_cores = "%s physical, %s logical" % (
            cpu_metadata["physical_cores"],
            cpu_metadata["logical_cores"],
        )
    lines.append("## Machine")
    lines.append("")
    lines.append("- OS: `%s %s`, `%s`" % (
        os_metadata["system"],
        os_metadata["release"],
        os_metadata["machine"],
    ))
    lines.append("- CPU: `%s`, %s cores" % (cpu_metadata["model"] or "unknown", cpu_cores))
    lines.append("- RAM: `%s`" % (memory_metadata["total_gib"] or "unknown"))
    lines.append("- Python: `%s`" % system_metadata["python"])
    lines.append("")


def git_value(repo, args):
    return run_command(["git", *args], cwd=repo).strip()


def repo_root():
    return Path(git_value(Path.cwd(), ["rev-parse", "--show-toplevel"]))


def short_hash(commit):
    return commit[:12]


def t_critical_975(df):
    table = {
        1: 12.706,
        2: 4.303,
        3: 3.182,
        4: 2.776,
        5: 2.571,
        6: 2.447,
        7: 2.365,
        8: 2.306,
        9: 2.262,
        10: 2.228,
        11: 2.201,
        12: 2.179,
        13: 2.160,
        14: 2.145,
        15: 2.131,
        16: 2.120,
        17: 2.110,
        18: 2.101,
        19: 2.093,
        20: 2.086,
        21: 2.080,
        22: 2.074,
        23: 2.069,
        24: 2.064,
        25: 2.060,
        26: 2.056,
        27: 2.052,
        28: 2.048,
        29: 2.045,
        30: 2.042,
    }
    if df <= 30:
        return table[max(1, df)]
    if df <= 40:
        return 2.021
    if df <= 60:
        return 2.000
    if df <= 120:
        return 1.980
    return 1.960


def summarize_timings(values, bytes_per_value, rows):
    count = len(values)
    mean_s = statistics.mean(values)
    median_s = statistics.median(values)
    stdev_s = statistics.stdev(values) if count > 1 else 0.0
    ci95_half_s = t_critical_975(count - 1) * stdev_s / math.sqrt(count) if count > 1 else 0.0
    total_mib = rows * bytes_per_value / 1024.0 / 1024.0
    ci95_low_s = mean_s - ci95_half_s
    ci95_high_s = mean_s + ci95_half_s
    mean_mib_s = total_mib / mean_s
    ci95_low_mib_s = total_mib / ci95_high_s
    ci95_high_mib_s = total_mib / ci95_low_s if ci95_low_s > 0 else None
    return {
        "sample_count": count,
        "mean_s": mean_s,
        "median_s": median_s,
        "min_s": min(values),
        "max_s": max(values),
        "stdev_s": stdev_s,
        "ci95_half_s": ci95_half_s,
        "ci95_low_s": ci95_low_s,
        "ci95_high_s": ci95_high_s,
        "total_mib": total_mib,
        "mean_mib_s": mean_mib_s,
        "ci95_low_mib_s": ci95_low_mib_s,
        "ci95_high_mib_s": ci95_high_mib_s,
    }


def parse_timing_output(output):
    timings = {benchmark_path: [] for benchmark_path in BENCHMARK_BY_PATH}
    for line in output.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        name, run_index, timing = parts
        if name == "name":
            continue
        if name not in timings:
            continue
        try:
            int(run_index)
            timings[name].append(float(timing))
        except ValueError:
            continue
    return timings


def create_worktree(repo, commit, path, force):
    target_head = git_value(repo, ["rev-parse", commit])
    if path.exists():
        existing_head = git_value(path, ["rev-parse", "HEAD"])
        if existing_head == target_head:
            return target_head
        if not force:
            raise RuntimeError("worktree %s is at %s, not %s; pass --force to recreate it" %
                               (path, existing_head, target_head))
        run_command(["git", "worktree", "remove", "--force", str(path)], cwd=repo)
    run_command(["git", "worktree", "add", "--detach", str(path), target_head], cwd=repo)
    return target_head


def build_benchmark_runner(worktree, result_dir, jobs, skip_build):
    runner = worktree / "build/release/benchmark/benchmark_runner"
    if skip_build:
        if not runner.exists():
            raise RuntimeError("benchmark runner is missing and --skip-build was used: %s" % runner)
        return runner
    env = os.environ.copy()
    env["CMAKE_BUILD_PARALLEL_LEVEL"] = str(jobs)
    run_command(["make", "benchmark"], cwd=worktree, env=env, log_path=result_dir / "build.log")
    if not runner.exists():
        raise RuntimeError("benchmark runner was not produced: %s" % runner)
    return runner


def collect_commit_metadata(worktree, commit):
    return {
        "commit": git_value(worktree, ["rev-parse", "HEAD"]),
        "input_commit": commit,
        "short_commit": git_value(worktree, ["rev-parse", "--short=12", "HEAD"]),
        "subject": git_value(worktree, ["log", "-1", "--format=%s"]),
        "author": git_value(worktree, ["log", "-1", "--format=%an <%ae>"]),
        "date": git_value(worktree, ["log", "-1", "--format=%cI"]),
    }


def run_benchmarks(runner, worktree, result_dir, invocations, threads, timeout):
    timings = {benchmark_path: [] for benchmark_path in BENCHMARK_BY_PATH}
    raw_dir = result_dir / "raw"
    for invocation in range(1, invocations + 1):
        print("[%s] invocation %02d/%02d" % (result_dir.name, invocation, invocations), flush=True)
        log_path = raw_dir / ("invocation-%02d.log" % invocation)
        output = run_command(
            [str(runner), BENCHMARK_PATTERN, "--disable-timeout", "--threads=%d" % threads],
            cwd=worktree,
            timeout=timeout,
            log_path=log_path,
        )
        invocation_timings = parse_timing_output(output)
        for benchmark_path in timings:
            values = invocation_timings[benchmark_path]
            if not values:
                raise RuntimeError("no timings found for %s in %s" % (benchmark_path, log_path))
            timings[benchmark_path].extend(values)
    return timings


def run_commit(repo, commit, label, out_dir, worktree_root, args):
    commit_short = short_hash(commit)
    worktree = worktree_root / ("append-zonemap-%s-%s" % (label, commit_short))
    result_dir = out_dir / ("%s-%s" % (label, commit_short))
    result_dir.mkdir(parents=True, exist_ok=True)

    print("[%s] preparing %s" % (label, commit_short), flush=True)
    create_worktree(repo, commit, worktree, args.force)
    metadata = collect_commit_metadata(worktree, commit)

    print("[%s] building %s" % (label, metadata["short_commit"]), flush=True)
    runner = build_benchmark_runner(worktree, result_dir, args.jobs, args.skip_build)

    print("[%s] running %s" % (label, metadata["short_commit"]), flush=True)
    timings = run_benchmarks(runner, worktree, result_dir, args.invocations, args.threads, args.timeout)

    benchmarks = []
    for type_name, benchmark_path, bytes_per_value in BENCHMARKS:
        benchmark_timings = timings[benchmark_path]
        benchmarks.append({
            "type": type_name,
            "benchmark": benchmark_path,
            "bytes_per_value": bytes_per_value,
            "rows": args.rows,
            "timings_s": benchmark_timings,
            "summary": summarize_timings(benchmark_timings, bytes_per_value, args.rows),
        })

    result = {
        "label": label,
        "metadata": metadata,
        "benchmark_runner": str(runner),
        "benchmarks": benchmarks,
    }
    write_json(result_dir / "results.json", result)
    write_timings_tsv(result_dir / "timings.tsv", result)
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")


def write_timings_tsv(path, commit_result):
    with open(path, "w") as f:
        f.write("commit\tbenchmark\ttype\tbytes_per_value\trun_index\ttiming_s\n")
        commit = commit_result["metadata"]["short_commit"]
        for benchmark in commit_result["benchmarks"]:
            for index, timing in enumerate(benchmark["timings_s"], start=1):
                f.write("%s\t%s\t%s\t%d\t%d\t%.9f\n" % (
                    commit,
                    benchmark["benchmark"],
                    benchmark["type"],
                    benchmark["bytes_per_value"],
                    index,
                    timing,
                ))


def benchmark_results_by_type(commit_result):
    return {benchmark["type"]: benchmark for benchmark in commit_result["benchmarks"]}


def format_mib_ci(summary):
    return "%.1f [%.1f, %.1f]" % (
        summary["mean_mib_s"],
        summary["ci95_low_mib_s"],
        summary["ci95_high_mib_s"],
    )


def format_ms_ci(summary):
    return "%.3f [%.3f, %.3f]" % (
        summary["mean_s"] * 1000.0,
        summary["ci95_low_s"] * 1000.0,
        summary["ci95_high_s"] * 1000.0,
    )


def comparison_rows(old_result, new_result):
    old_by_type = benchmark_results_by_type(old_result)
    new_by_type = benchmark_results_by_type(new_result)
    rows = []
    for type_name, _, bytes_per_value in BENCHMARKS:
        old_benchmark = old_by_type[type_name]
        new_benchmark = new_by_type[type_name]
        old_summary = old_benchmark["summary"]
        new_summary = new_benchmark["summary"]
        improvement = new_summary["mean_mib_s"] / old_summary["mean_mib_s"]
        rows.append({
            "type": type_name,
            "bytes_per_value": bytes_per_value,
            "sample_count": old_summary["sample_count"],
            "old": old_summary,
            "new": new_summary,
            "improvement": improvement,
        })
    return rows


def build_comparison_table(old_result, new_result):
    lines = []
    lines.append("| Type | Bytes/value | n | Old time ms (95% CI) | New time ms (95% CI) | "
                 "Old MiB/s (95% CI) | New MiB/s (95% CI) | Improvement |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in comparison_rows(old_result, new_result):
        old_summary = row["old"]
        new_summary = row["new"]
        lines.append("| %s | %d | %d | %s | %s | %s | %s | %.2fx |" % (
            row["type"],
            row["bytes_per_value"],
            row["sample_count"],
            format_ms_ci(old_summary),
            format_ms_ci(new_summary),
            format_mib_ci(old_summary),
            format_mib_ci(new_summary),
            row["improvement"],
        ))
    return "\n".join(lines)


def write_summary_tsv(path, old_result, new_result):
    with open(path, "w") as f:
        f.write("type\tbytes_per_value\tn\told_time_s\told_time_ci95_half_s\tnew_time_s\t"
                "new_time_ci95_half_s\told_mib_s\told_mib_s_ci95_low\told_mib_s_ci95_high\t"
                "new_mib_s\tnew_mib_s_ci95_low\tnew_mib_s_ci95_high\timprovement_x\n")
        for row in comparison_rows(old_result, new_result):
            old_summary = row["old"]
            new_summary = row["new"]
            f.write("%s\t%d\t%d\t%.9f\t%.9f\t%.9f\t%.9f\t%.6f\t%.6f\t%.6f\t"
                    "%.6f\t%.6f\t%.6f\t%.6f\n" % (
                        row["type"],
                        row["bytes_per_value"],
                        row["sample_count"],
                        old_summary["mean_s"],
                        old_summary["ci95_half_s"],
                        new_summary["mean_s"],
                        new_summary["ci95_half_s"],
                        old_summary["mean_mib_s"],
                        old_summary["ci95_low_mib_s"],
                        old_summary["ci95_high_mib_s"],
                        new_summary["mean_mib_s"],
                        new_summary["ci95_low_mib_s"],
                        new_summary["ci95_high_mib_s"],
                        row["improvement"],
                    ))


def write_summary_markdown(path, old_result, new_result, system_metadata, metadata):
    lines = []
    lines.append("# Append Numeric Zonemap Benchmark Results")
    lines.append("")
    append_system_summary(lines, system_metadata)
    lines.append("## Run")
    lines.append("")
    lines.append("- Benchmark pattern: `%s`" % BENCHMARK_PATTERN)
    lines.append("- Rows per benchmark: `%d`" % metadata["configuration"]["rows"])
    lines.append("- Invocations per commit: `%d`" % metadata["configuration"]["invocations"])
    lines.append("- DuckDB benchmark threads: `%d`" % metadata["configuration"]["threads"])
    lines.append("")
    lines.append("## Commits")
    lines.append("")
    lines.append("| Label | Commit | Subject |")
    lines.append("|---|---|---|")
    for result in (old_result, new_result):
        commit = result["metadata"]["short_commit"]
        subject = result["metadata"]["subject"].replace("|", "\\|")
        lines.append("| %s | `%s` | %s |" % (result["label"], commit, subject))
    lines.append("")
    lines.append("## Comparison")
    lines.append("")
    lines.append(build_comparison_table(old_result, new_result))
    lines.append("")
    lines.append("MiB/s is computed from `rows * physical_width / mean_time / 1024^2`. "
                 "Throughput confidence intervals are derived from the 95% confidence interval for mean time.")
    lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build two DuckDB commits and compare SQL append numeric zonemap benchmark throughput."
    )
    parser.add_argument("--old-commit", default=DEFAULT_OLD_COMMIT,
                        help="Baseline commit. Defaults to the SQL benchmark commit.")
    parser.add_argument("--new-commit", default=DEFAULT_NEW_COMMIT,
                        help="Optimized commit. Defaults to Optimize contiguous fixed-size zonemap creation.")
    parser.add_argument("--output-dir", default="append-zonemap-benchmark-results",
                        help="Directory for results, logs, and summary tables.")
    parser.add_argument("--worktree-root", default=None,
                        help="Directory for temporary git worktrees. Defaults to a directory under TMPDIR.")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="Build parallelism via CMAKE_BUILD_PARALLEL_LEVEL.")
    parser.add_argument("--threads", type=int, default=1,
                        help="DuckDB benchmark_runner --threads value.")
    parser.add_argument("--invocations", type=int, default=6,
                        help="How many times to invoke benchmark_runner per commit. Each invocation normally "
                             "produces 5 hot timings per benchmark, so the default produces n=30.")
    parser.add_argument("--timeout", type=int, default=1200,
                        help="Timeout in seconds per benchmark_runner invocation.")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS,
                        help="Rows inserted by each benchmark. This must match the benchmark file argument.")
    parser.add_argument("--skip-build", action="store_true",
                        help="Reuse existing worktree build/release/benchmark/benchmark_runner binaries.")
    parser.add_argument("--force", action="store_true",
                        help="Recreate existing worktrees if they are at different commits.")
    return parser.parse_args()


def main():
    args = parse_args()
    repo = repo_root()
    out_dir = (repo / args.output_dir).resolve()
    if args.worktree_root:
        worktree_root = Path(args.worktree_root).resolve()
    else:
        tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
        worktree_root = tmpdir / "duckdb-append-zonemap-benchmark-worktrees"
    out_dir.mkdir(parents=True, exist_ok=True)
    worktree_root.mkdir(parents=True, exist_ok=True)

    system_metadata = collect_system_metadata()
    metadata = {
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(repo),
        "output_dir": str(out_dir),
        "worktree_root": str(worktree_root),
        "benchmark_pattern": BENCHMARK_PATTERN,
        "system": system_metadata,
        "configuration": vars(args),
    }
    write_json(out_dir / "metadata.json", metadata)

    old_result = run_commit(repo, args.old_commit, "old", out_dir, worktree_root, args)
    new_result = run_commit(repo, args.new_commit, "new", out_dir, worktree_root, args)
    results = {
        "metadata": metadata,
        "old": old_result,
        "new": new_result,
    }
    write_json(out_dir / "results.json", results)
    write_summary_tsv(out_dir / "summary.tsv", old_result, new_result)
    write_summary_markdown(out_dir / "summary.md", old_result, new_result, system_metadata, metadata)

    table = build_comparison_table(old_result, new_result)
    system_lines = []
    append_system_summary(system_lines, system_metadata)
    print("")
    print("\n".join(system_lines))
    print(table)
    print("")
    print("wrote %s" % (out_dir / "summary.md"), flush=True)


if __name__ == "__main__":
    main()
