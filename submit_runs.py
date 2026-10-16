#!/usr/bin/env python3
"""Queues SCOPE runs for the experiment through the REST API.

Usage:
  python3 submit_runs.py --env-file <env file> --owner <email> --prompt p1-push-optional \\
      --profiles copilotCli-claude-sonnet-55-github copilotCli-claude-sonnet-55-local [--batches 1]

  --profiles all   queues every profile in imported.json

Runs for a prompt go into one benchmark, created on first use and reused while
the prompt's scenario and criteria set stay the same. A new scenario or
criteria set starts a new benchmark. --label keeps runs in a separate benchmark
(e.g. --label smoke), recorded in imported.json under "<prompt>@<label>". Each batch is 5
runs per profile. Benchmark and run IDs are recorded in imported.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from import_definitions import ROOT, api_headers, request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--prompt", required=True, choices=["p1-push-optional", "p2-push-requested"])
    parser.add_argument("--profiles", nargs="+", required=True)
    parser.add_argument("--batches", type=int, default=1)
    parser.add_argument("--label", help="Keeps these runs in their own benchmark, e.g. 'smoke' or 'final'")
    args = parser.parse_args()

    imported_path = ROOT / "imported.json"
    if not imported_path.exists():
        sys.exit("Run import_definitions.py first to create the local imported.json state.")
    imported = json.loads(imported_path.read_text(encoding="utf-8"))
    authority, headers = api_headers(args.env_file, args.owner)
    if imported.get("authority") != authority:
        sys.exit("The env file targets a different API. Re-import definitions for that API before submitting runs.")

    scenario_id = imported["scenarios"][args.prompt]["id"]
    criteria_id = imported["criteria"][args.prompt]["id"]
    profile_keys = sorted(imported["profiles"]) if args.profiles == ["all"] else args.profiles
    unknown = [key for key in profile_keys if key not in imported["profiles"]]
    if unknown:
        sys.exit(f"Unknown profiles: {', '.join(unknown)}")

    benchmarks = imported.setdefault("benchmarks", {})
    benchmark_key = f"{args.prompt}@{args.label}" if args.label else args.prompt
    benchmark = benchmarks.get(benchmark_key)
    if not benchmark or benchmark.get("scenarioId") != scenario_id or benchmark.get("criteriaId") != criteria_id:
        # A changed scenario or criteria set starts a new benchmark so results aren't mixed.
        if benchmark:
            imported.setdefault("previousBenchmarks", []).append({"prompt": args.prompt, "key": benchmark_key, **benchmark})
        criteria_name = imported["criteria"][args.prompt]["name"]
        suffix = criteria_name.rsplit(' ', 1)[-1] + (f", {args.label}" if args.label else "")
        name = f"Git remote eval: {args.prompt} ({suffix})"
        status, response = request("POST", f"{authority}/api/benchmarks", headers, {"scenarioId": scenario_id, "name": name})
        if status != 201:
            sys.exit(f"Creating benchmark failed with {status}: {response}")
        benchmark = {"id": response["benchmarkId"], "name": name, "scenarioId": scenario_id,
                     "criteriaId": criteria_id, "runs": {}}
        benchmarks[benchmark_key] = benchmark
        print(f"created benchmark {benchmark['id']} ({name})")
    else:
        print(f"using benchmark {benchmark['id']} ({benchmark['name']})")

    failures = 0
    for key in profile_keys:
        for _ in range(args.batches):
            status, response = request("POST", f"{authority}/api/runs", headers, {
                "profileId": imported["profiles"][key]["id"],
                "scenarioId": scenario_id,
                "criterionId": criteria_id,
                "benchmarkId": benchmark["id"],
            })
            if status == 202:
                benchmark["runs"].setdefault(key, []).extend(response["runIds"])
                print(f"queued {len(response['runIds'])} runs for {key}")
            else:
                failures += 1
                print(f"FAILED  {key}: {status} {str(response)[:300]}")
        imported_path.write_text(json.dumps(imported, indent=2) + "\n", encoding="utf-8")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
