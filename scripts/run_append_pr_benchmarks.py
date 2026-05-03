#!/usr/bin/env python3
import argparse
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path


APPEND_DATA_CHUNK_ROWS = 1048576
TYPE_BYTES = {
    "u8": 1,
    "i8": 1,
    "u16": 2,
    "i16": 2,
    "u32": 4,
    "i32": 4,
    "f32": 4,
    "f64": 8,
}

STACKS = [
    {
        "name": "appender-parser-cache",
        "commits": [
            "bc21913f39cb632894c4c97258aa09a922ced79b",
            "a1e99549bab786b06f0d158df07c461e4186da16",
            "0d2c5c1d2b3ef8aa574a699d0eef9ae5752e65ac",
        ],
        "benchmarks": [
            "AppendDataChunkI32AllValid1ColFlushEvery128Rows",
        ],
        "benchmark_overlays": [
            "benchmark/micro/append.cpp",
        ],
    },
    {
        "name": "contiguous-fixed-size-zonemap",
        "commits": [
            "b5ce5462678623ae9c69a5beff775a4d38c9cecf",
            "f5455f9856f5449cc9b4d405b3514cfffdf9f349",
        ],
        "benchmarks": [
            "AppendDataChunkU32AllValid1Col",
            "AppendDataChunkU32WithNulls1Col",
            "AppendDataChunkU16AllValid1Col",
            "AppendDataChunkU16WithNulls1Col",
            "AppendDataChunkU8AllValid1Col",
            "AppendDataChunkU8WithNulls1Col",
            "AppendDataChunkI32AllValid1Col",
            "AppendDataChunkI32WithNulls1Col",
            "AppendDataChunkI16AllValid1Col",
            "AppendDataChunkI16WithNulls1Col",
            "AppendDataChunkI8AllValid1Col",
            "AppendDataChunkI8WithNulls1Col",
            "AppendDataChunkF32AllValid1Col",
            "AppendDataChunkF32WithNulls1Col",
            "AppendDataChunkF64AllValid1Col",
            "AppendDataChunkF64WithNulls1Col",
        ],
        "benchmark_overlays": [
            "benchmark/micro/append.cpp",
        ],
    },
]


def run_command(args, cwd, timeout=None, env=None, log_path=None):
    start = time.time()
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)
    elapsed = time.time() - start
    output = proc.stdout + proc.stderr
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as f:
            f.write("$ ")
            f.write(" ".join(args))
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
        raise RuntimeError("command failed: %s\n%s" % (" ".join(args), detail))
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


def parse_timings(output, benchmark_name):
    timings = []
    for line in output.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        name, run_index, timing = parts
        if name != benchmark_name:
            continue
        try:
            int(run_index)
            timings.append(float(timing))
        except ValueError:
            continue
    if not timings:
        raise RuntimeError("no timings found for benchmark %s" % benchmark_name)
    return timings


def summarize(values):
    count = len(values)
    mean = statistics.mean(values)
    median = statistics.median(values)
    stdev = statistics.stdev(values) if count > 1 else 0.0
    ci_delta = t_critical_975(count - 1) * stdev / math.sqrt(count) if count > 1 else 0.0
    return {
        "sample_count": count,
        "mean_s": mean,
        "median_s": median,
        "min_s": min(values),
        "max_s": max(values),
        "stdev_s": stdev,
        "ci95_low_s": mean - ci_delta,
        "ci95_high_s": mean + ci_delta,
    }


def describe_benchmark(name):
    description = {
        "type": "",
        "validity": "",
        "flush": "",
    }
    prefix = "AppendDataChunk"
    if not name.startswith(prefix):
        return description

    body = name[len(prefix):]
    if "AllValid" in body:
        type_name, rest = body.split("AllValid", 1)
        description["type"] = type_name.lower()
        description["validity"] = "all valid"
    elif "WithNulls" in body:
        type_name, rest = body.split("WithNulls", 1)
        description["type"] = type_name.lower()
        description["validity"] = "with nulls"
    else:
        return description

    if "FlushEvery" in rest:
        description["flush"] = rest.split("FlushEvery", 1)[1]
    return description


def speedup(previous_mean, current_mean):
    if previous_mean == 0:
        return 1.0
    return previous_mean / current_mean


def mb_per_second(type_name, mean_s):
    bytes_per_value = TYPE_BYTES.get(type_name)
    if not bytes_per_value or mean_s == 0:
        return None
    return APPEND_DATA_CHUNK_ROWS * bytes_per_value / mean_s / 1000000.0


def format_optional_float(value, precision):
    if value is None:
        return ""
    return ("%." + str(precision) + "f") % value


def format_optional_speedup(value):
    if value is None:
        return ""
    return "%.2fx" % value


def benchmark_results_by_name(commit_result):
    return {benchmark["name"]: benchmark for benchmark in commit_result["benchmarks"]}


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


def create_worktree(repo, commit, path, force):
    if path.exists():
        existing_head = git_value(path, ["rev-parse", "HEAD"])
        if existing_head == commit:
            return
        if not force:
            raise RuntimeError("worktree %s is at %s, not %s; pass --force to recreate it" %
                               (path, existing_head, commit))
        run_command(["git", "worktree", "remove", "--force", str(path)], cwd=repo)
    run_command(["git", "worktree", "add", "--detach", str(path), commit], cwd=repo)


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


def apply_benchmark_overlays(repo, worktree, result_dir, overlay_paths):
    if not overlay_paths:
        return
    log_lines = []
    for overlay_path in overlay_paths:
        source = repo / overlay_path
        destination = worktree / overlay_path
        if not source.exists():
            raise RuntimeError("benchmark overlay source does not exist: %s" % source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        log_lines.append("%s <- %s" % (destination, source))
    with open(result_dir / "benchmark-overlays.txt", "w") as f:
        f.write("\n".join(log_lines))
        f.write("\n")


def benchmark_exists(runner, benchmark_name, result_dir, timeout):
    output = run_command([str(runner), "--list"], cwd=runner.parents[3], timeout=timeout,
                         log_path=result_dir / "benchmark-list.txt")
    names = {line.strip() for line in output.splitlines() if line.strip()}
    if benchmark_name not in names:
        raise RuntimeError("benchmark %s was not listed by %s" % (benchmark_name, runner))


def run_benchmark(runner, benchmark_name, result_dir, invocations, threads, timeout):
    timings = []
    raw_dir = result_dir / "raw"
    for invocation in range(1, invocations + 1):
        log_path = raw_dir / ("%s.invocation-%02d.txt" % (benchmark_name, invocation))
        output = run_command([str(runner), benchmark_name, "--threads=%d" % threads],
                             cwd=runner.parents[3], timeout=timeout, log_path=log_path)
        timings.extend(parse_timings(output, benchmark_name))
    return {
        "name": benchmark_name,
        "timings_s": timings,
        "summary": summarize(timings),
    }


def collect_commit_metadata(worktree, commit):
    return {
        "commit": commit,
        "short_commit": git_value(worktree, ["rev-parse", "--short=12", "HEAD"]),
        "subject": git_value(worktree, ["log", "-1", "--format=%s"]),
        "author": git_value(worktree, ["log", "-1", "--format=%an <%ae>"]),
        "date": git_value(worktree, ["log", "-1", "--format=%cI"]),
    }


def run_stack(repo, stack, out_dir, worktree_root, args, system_metadata):
    stack_dir = out_dir / stack["name"]
    stack_dir.mkdir(parents=True, exist_ok=True)
    stack_results = {
        "name": stack["name"],
        "benchmarks": stack["benchmarks"],
        "system": system_metadata,
        "commits": [],
    }
    for commit in stack["commits"]:
        commit_short = short_hash(commit)
        worktree = worktree_root / ("%s-%s" % (stack["name"], commit_short))
        result_dir = stack_dir / commit_short
        result_dir.mkdir(parents=True, exist_ok=True)

        print("[%s] preparing %s" % (stack["name"], commit_short), flush=True)
        create_worktree(repo, commit, worktree, args.force)
        metadata = collect_commit_metadata(worktree, commit)
        apply_benchmark_overlays(repo, worktree, result_dir, stack.get("benchmark_overlays", []))

        print("[%s] building %s" % (stack["name"], commit_short), flush=True)
        runner = build_benchmark_runner(worktree, result_dir, args.jobs, args.skip_build)

        benchmark_results = []
        for benchmark_name in stack["benchmarks"]:
            print("[%s] %s: %s" % (stack["name"], commit_short, benchmark_name), flush=True)
            benchmark_exists(runner, benchmark_name, result_dir, args.timeout)
            benchmark_results.append(run_benchmark(runner, benchmark_name, result_dir, args.invocations,
                                                   args.threads, args.timeout))

        commit_result = {
            "metadata": metadata,
            "benchmark_runner": str(runner),
            "benchmarks": benchmark_results,
        }
        write_json(result_dir / "results.json", commit_result)
        write_tsv(result_dir / "timings.tsv", commit_result)
        stack_results["commits"].append(commit_result)

    write_json(stack_dir / "results.json", stack_results)
    write_stack_markdown(stack_dir / "summary.md", stack_results)
    write_stack_tsv(stack_dir / "summary.tsv", stack_results)
    write_stack_comparison_tsv(stack_dir / "comparisons.tsv", stack_results)
    return stack_results


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")


def write_tsv(path, commit_result):
    with open(path, "w") as f:
        f.write("commit\tbenchmark\trun_index\ttiming_s\n")
        commit = commit_result["metadata"]["short_commit"]
        for benchmark in commit_result["benchmarks"]:
            for index, timing in enumerate(benchmark["timings_s"], start=1):
                f.write("%s\t%s\t%d\t%.9f\n" % (commit, benchmark["name"], index, timing))


def write_stack_tsv(path, stack_result):
    with open(path, "w") as f:
        f.write("commit\tsubject\tbenchmark\ttype\tvalidity\tflush\trows\tsamples\tmean_s\tmedian_s\tci95_low_s\t"
                "ci95_high_s\trows_per_s\tmb_s\tspeedup_vs_previous\n")
        previous = {}
        for commit_result in stack_result["commits"]:
            commit = commit_result["metadata"]["short_commit"]
            subject = commit_result["metadata"]["subject"]
            for benchmark in commit_result["benchmarks"]:
                summary = benchmark["summary"]
                name = benchmark["name"]
                benchmark_description = describe_benchmark(name)
                relative_speed = speedup(previous.get(name, summary["mean_s"]), summary["mean_s"])
                previous[name] = summary["mean_s"]
                mb_s = mb_per_second(benchmark_description["type"], summary["mean_s"])
                f.write("%s\t%s\t%s\t%s\t%s\t%s\t%d\t%d\t%.9f\t%.9f\t%.9f\t%.9f\t%.3f\t%s\t%.3f\n" % (
                    commit,
                    subject,
                    name,
                    benchmark_description["type"],
                    benchmark_description["validity"],
                    benchmark_description["flush"],
                    APPEND_DATA_CHUNK_ROWS,
                    summary["sample_count"],
                    summary["mean_s"],
                    summary["median_s"],
                    summary["ci95_low_s"],
                    summary["ci95_high_s"],
                    APPEND_DATA_CHUNK_ROWS / summary["mean_s"],
                    format_optional_float(mb_s, 3),
                    relative_speed,
                ))


def write_stack_comparison_tsv(path, stack_result):
    with open(path, "w") as f:
        f.write("old_commit\tnew_commit\tbenchmark\ttype\tvalidity\trows\told_mb_s\tnew_mb_s\timprovement\n")
        for commit_index in range(1, len(stack_result["commits"])):
            old_commit_result = stack_result["commits"][commit_index - 1]
            new_commit_result = stack_result["commits"][commit_index]
            old_commit = old_commit_result["metadata"]["short_commit"]
            new_commit = new_commit_result["metadata"]["short_commit"]
            old_results = benchmark_results_by_name(old_commit_result)
            new_results = benchmark_results_by_name(new_commit_result)
            for name in stack_result["benchmarks"]:
                if name not in old_results or name not in new_results:
                    continue
                benchmark_description = describe_benchmark(name)
                old_summary = old_results[name]["summary"]
                new_summary = new_results[name]["summary"]
                old_mb_s = mb_per_second(benchmark_description["type"], old_summary["mean_s"])
                new_mb_s = mb_per_second(benchmark_description["type"], new_summary["mean_s"])
                if old_mb_s is None or old_mb_s == 0 or new_mb_s is None:
                    improvement = None
                else:
                    improvement = new_mb_s / old_mb_s
                f.write("%s\t%s\t%s\t%s\t%s\t%d\t%s\t%s\t%s\n" % (
                    old_commit,
                    new_commit,
                    name,
                    benchmark_description["type"],
                    benchmark_description["validity"],
                    APPEND_DATA_CHUNK_ROWS,
                    format_optional_float(old_mb_s, 3),
                    format_optional_float(new_mb_s, 3),
                    format_optional_float(improvement, 3),
                ))


def write_stack_markdown(path, stack_result):
    lines = []
    lines.append("# %s Benchmark Results" % stack_result["name"])
    lines.append("")
    append_system_summary(lines, stack_result["system"])
    lines.append("## Commits")
    lines.append("")
    lines.append("| Commit | Subject |")
    lines.append("|---|---|")
    for commit_result in stack_result["commits"]:
        commit = commit_result["metadata"]["short_commit"]
        subject = commit_result["metadata"]["subject"].replace("|", "\\|")
        lines.append("| `%s` | %s |" % (commit, subject))
    lines.append("")
    lines.append("## Timings")
    lines.append("")
    lines.append("| Commit | Benchmark | Type | Validity | Rows | Samples | Mean ms | 95% CI ms | "
                 "Rows/s | MB/s | Speedup vs previous |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    previous = {}
    for commit_result in stack_result["commits"]:
        commit = commit_result["metadata"]["short_commit"]
        for benchmark in commit_result["benchmarks"]:
            summary = benchmark["summary"]
            name = benchmark["name"]
            benchmark_description = describe_benchmark(name)
            relative_speed = speedup(previous.get(name, summary["mean_s"]), summary["mean_s"])
            previous[name] = summary["mean_s"]
            mb_s = mb_per_second(benchmark_description["type"], summary["mean_s"])
            lines.append("| `%s` | `%s` | %s | %s | %d | %d | %.3f | [%.3f, %.3f] | %.0f | %s | %.2fx |" % (
                commit,
                name,
                benchmark_description["type"],
                benchmark_description["validity"],
                APPEND_DATA_CHUNK_ROWS,
                summary["sample_count"],
                summary["mean_s"] * 1000.0,
                summary["ci95_low_s"] * 1000.0,
                summary["ci95_high_s"] * 1000.0,
                APPEND_DATA_CHUNK_ROWS / summary["mean_s"],
                format_optional_float(mb_s, 1),
                relative_speed,
            ))
    lines.append("")
    lines.append("`Speedup vs previous` is computed per benchmark. The first commit for each benchmark is `1.00x`.")
    lines.append("")
    if len(stack_result["commits"]) > 1:
        lines.append("## Comparisons")
        lines.append("")
        lines.append("| Old | New | Benchmark | Type | Validity | Old MB/s | New MB/s | Improvement |")
        lines.append("|---|---|---|---|---|---:|---:|---:|")
        for commit_index in range(1, len(stack_result["commits"])):
            old_commit_result = stack_result["commits"][commit_index - 1]
            new_commit_result = stack_result["commits"][commit_index]
            old_commit = old_commit_result["metadata"]["short_commit"]
            new_commit = new_commit_result["metadata"]["short_commit"]
            old_results = benchmark_results_by_name(old_commit_result)
            new_results = benchmark_results_by_name(new_commit_result)
            for name in stack_result["benchmarks"]:
                if name not in old_results or name not in new_results:
                    continue
                benchmark_description = describe_benchmark(name)
                old_summary = old_results[name]["summary"]
                new_summary = new_results[name]["summary"]
                old_mb_s = mb_per_second(benchmark_description["type"], old_summary["mean_s"])
                new_mb_s = mb_per_second(benchmark_description["type"], new_summary["mean_s"])
                if old_mb_s is None or old_mb_s == 0 or new_mb_s is None:
                    improvement = None
                else:
                    improvement = new_mb_s / old_mb_s
                lines.append("| `%s` | `%s` | `%s` | %s | %s | %s | %s | %s |" % (
                    old_commit,
                    new_commit,
                    name,
                    benchmark_description["type"],
                    benchmark_description["validity"],
                    format_optional_float(old_mb_s, 1),
                    format_optional_float(new_mb_s, 1),
                    format_optional_speedup(improvement),
                ))
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines))


def write_overall_summary(out_dir, results, system_metadata):
    lines = []
    lines.append("# Append PR Benchmark Results")
    lines.append("")
    append_system_summary(lines, system_metadata)
    for result in results:
        lines.append("## %s" % result["name"])
        lines.append("")
        lines.append("See `%s/summary.md`, `%s/summary.tsv`, and `%s/comparisons.tsv`." % (
            result["name"],
            result["name"],
            result["name"],
        ))
        lines.append("")
    with open(out_dir / "summary.md", "w") as f:
        f.write("\n".join(lines))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build a fixed stack of DuckDB append benchmark commits and capture benchmark timings."
    )
    parser.add_argument("--output-dir", default="append-pr-benchmark-results",
                        help="Directory for all results and logs.")
    parser.add_argument("--worktree-root", default=None,
                        help="Directory for temporary git worktrees. Defaults to a directory under TMPDIR.")
    parser.add_argument("--stack", action="append", choices=[stack["name"] for stack in STACKS],
                        help="Stack to run. Repeatable. Defaults to both stacks.")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="Build parallelism via CMAKE_BUILD_PARALLEL_LEVEL.")
    parser.add_argument("--threads", type=int, default=1,
                        help="DuckDB benchmark_runner --threads value.")
    parser.add_argument("--invocations", type=int, default=3,
                        help="How many times to invoke benchmark_runner per benchmark. Each invocation includes the "
                             "benchmark's built-in hot runs.")
    parser.add_argument("--timeout", type=int, default=900,
                        help="Timeout in seconds per benchmark_runner invocation.")
    parser.add_argument("--skip-build", action="store_true",
                        help="Reuse existing worktree build/release/benchmark/benchmark_runner binaries.")
    parser.add_argument("--force", action="store_true",
                        help="Recreate existing worktrees for the same commits.")
    return parser.parse_args()


def main():
    args = parse_args()
    repo = repo_root()
    out_dir = (repo / args.output_dir).resolve()
    if args.worktree_root:
        worktree_root = Path(args.worktree_root).resolve()
    else:
        tmpdir = Path(os.environ.get("TMPDIR", "/tmp"))
        worktree_root = tmpdir / "duckdb-append-pr-benchmark-worktrees"
    out_dir.mkdir(parents=True, exist_ok=True)
    worktree_root.mkdir(parents=True, exist_ok=True)

    selected = set(args.stack or [stack["name"] for stack in STACKS])
    stacks = [stack for stack in STACKS if stack["name"] in selected]
    if not stacks:
        raise RuntimeError("no stacks selected")

    system_metadata = collect_system_metadata()
    metadata = {
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(repo),
        "output_dir": str(out_dir),
        "worktree_root": str(worktree_root),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "system": system_metadata,
        "configuration": vars(args),
    }
    write_json(out_dir / "metadata.json", metadata)

    results = []
    for stack in stacks:
        results.append(run_stack(repo, stack, out_dir, worktree_root, args, system_metadata))
    write_json(out_dir / "results.json", {"metadata": metadata, "stacks": results})
    write_overall_summary(out_dir, results, system_metadata)
    print("wrote %s" % (out_dir / "summary.md"), flush=True)


if __name__ == "__main__":
    main()
