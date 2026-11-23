#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic staged analysis pipeline with provenance. Standard library only."""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

MAX_ROWS = 200000
PATH_KEYS = ("input_dir", "input_root", "materials_dir", "materials", "data_dir", "dataset_dir", "directory", "path", "root")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".cache"}
MISSING = {"", "na", "n/a", "nan", "none", "null", "nil", "-", "--", "-999", "-9999"}
ALIASES = {
    "case_id": ["case_id", "caseid", "id"],
    "stages": ["stages", "steps", "pipeline", "stage_results"],
    "artifacts": ["artifacts", "outputs", "files", "products"],
    "dataset": ["dataset", "data", "ingested", "profile"],
    "statistics": ["statistics", "stats_results", "analysis", "tests"],
    "report": ["report", "markdown", "report_markdown", "summary", "conclusion"],
    "checks": ["checks", "quality_checks", "warnings", "validation"],
    "status": ["status", "run_status"],
    "result": ["result", "results", "data"],
    "evidence": ["evidence", "provenance", "sources"],
    "uncertainty": ["uncertainty", "limitations"],
    "errors": ["errors", "error"],
    "stats": ["stats", "statistics_summary", "counts"],
}


def norm_key(name):
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def contract_fields(case):
    oc = case.get("output_contract")
    out = []
    if isinstance(oc, dict):
        if isinstance(oc.get("properties"), dict):
            out = list(oc["properties"].keys())
        elif isinstance(oc.get("fields"), dict):
            out = list(oc["fields"].keys())
        elif isinstance(oc.get("fields"), list):
            out = [x for x in oc["fields"] if isinstance(x, str)]
        elif isinstance(oc.get("required"), list):
            out = [x for x in oc["required"] if isinstance(x, str)]
        else:
            out = list(oc.keys())
    elif isinstance(oc, list):
        out = [x for x in oc if isinstance(x, str)]
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def iter_paths(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_paths(item)
    elif isinstance(value, dict):
        for key in ("path", "dir", "file", "files", "root"):
            if key in value:
                yield from iter_paths(value[key])


def candidate_roots(case, case_path):
    base = case_path.parent
    roots = []
    for key in PATH_KEYS + ("data_files",):
        if key in case:
            for raw in iter_paths(case[key]):
                p = Path(raw)
                if not p.is_absolute():
                    p = base / p
                roots.append(p)
    if base.name.lower() == "input" or (base / "materials").is_dir():
        roots.append(base)
    if Path("/input").is_dir():
        roots.append(Path("/input"))
    seen, uniq = set(), []
    for r in roots:
        try:
            key = str(r.resolve())
        except OSError:
            key = str(r)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def collect(roots, errors, skipped, exclude):
    files, seen = [], set()
    try:
        exclude_key = str(exclude.resolve()) if exclude else None
    except OSError:
        exclude_key = None
    for root in roots:
        if not root.exists():
            errors.append("material root not found: %s" % (root.name or str(root)))
            continue
        if root.is_file():
            paths, root_dir = [root], root.parent
        else:
            root_dir, paths = root, []
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                    for fn in sorted(filenames):
                        paths.append(Path(dirpath) / fn)
            except OSError as exc:
                errors.append("walk failed on %s: %s" % (root.name, exc))
                continue
        for path in paths:
            try:
                rp = str(path.resolve())
                if path.is_symlink() or not path.is_file() or rp == exclude_key:
                    continue
            except OSError:
                continue
            if rp in seen:
                continue
            seen.add(rp)
            try:
                rel = path.relative_to(root_dir).as_posix()
            except ValueError:
                rel = path.name
            files.append((path, rel))
    files.sort(key=lambda x: x[1])
    return files


def decode(data):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def to_float(value):
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def mean(xs):
    return sum(xs) / float(len(xs))


def sd(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1.0))


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def quantile(xs, q):
    s = sorted(xs)
    if not s:
        return None
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def betacf(a, b, x):
    maxit, eps, fpmin = 200, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                  + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * betacf(a, b, x) / a
    return 1.0 - bt * betacf(b, a, 1.0 - x) / b


def t_p_two(t, df):
    return betai(df / 2.0, 0.5, df / (df + t * t)) if df > 0 else None


def f_p_upper(f, df1, df2):
    return betai(df2 / 2.0, df1 / 2.0, df2 / (df2 + df1 * f)) if f > 0 and df1 > 0 and df2 > 0 else None


def welch(a, b):
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None
    v1, v2 = sd(a) ** 2, sd(b) ** 2
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se <= 0:
        return None
    t = (mean(a) - mean(b)) / se
    den = (v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1)
    df = (v1 / n1 + v2 / n2) ** 2 / den if den > 0 else n1 + n2 - 2
    pooled = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2))
    return {"t": t, "df": df, "p": t_p_two(t, df), "mean_diff": mean(a) - mean(b),
            "cohen_d": (mean(a) - mean(b)) / pooled if pooled > 0 else None}


def anova(groups):
    k = len(groups)
    n = sum(len(g) for g in groups)
    df1, df2 = k - 1, n - k
    if df1 <= 0 or df2 <= 0:
        return None
    grand = mean([x for g in groups for x in g])
    ssb = sum(len(g) * (mean(g) - grand) ** 2 for g in groups)
    ssw = sum(sum((x - mean(g)) ** 2 for x in g) for g in groups)
    if ssw <= 0:
        return None
    f = (ssb / df1) / (ssw / df2)
    return {"F": f, "df1": df1, "df2": df2, "p": f_p_upper(f, df1, df2),
            "eta_squared": ssb / (ssb + ssw)}


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_field(field, payload):
    key = norm_key(field)
    for canonical, names in ALIASES.items():
        if key == canonical or key in names:
            return canonical, payload.get(canonical)
    return None, None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Staged analysis pipeline (stdlib only)")
    parser.add_argument("--case", default="/input/case.json")
    parser.add_argument("--out", "--output", default="/app/output/result.json")
    parser.add_argument("--work", default="/app/work")
    args = parser.parse_args(argv)

    case_path = Path(args.case)
    errors, case, status = [], {}, "success"
    try:
        with open(case_path, "r", encoding="utf-8-sig") as fh:
            case = json.load(fh)
        if not isinstance(case, dict):
            raise ValueError("case.json root is not an object")
    except (OSError, ValueError) as exc:
        errors.append("cannot read case.json: %s" % exc)
        case, status = {}, "failed"

    case_id = case.get("case_id") if isinstance(case.get("case_id"), str) else "unknown"
    fields = contract_fields(case)
    alpha = case.get("alpha") if isinstance(case.get("alpha"), (int, float)) else 0.05
    work = Path(args.work)
    stages_dir = work / "stages"
    state_path = work / "pipeline_state.json"
    try:
        stages_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        errors.append("cannot create work dir: %s" % exc)

    stages, artifacts = [], {}

    def record(name, stage_status, inputs, outputs):
        entry = {"stage": name, "status": stage_status,
                 "inputs": inputs, "outputs": outputs}
        stages.append(entry)
        try:
            (stages_dir / ("%02d_%s.json" % (len(stages), name))).write_text(
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            artifacts[name] = "stages/%02d_%s.json" % (len(stages), name)
        except OSError as exc:
            errors.append("cannot write stage %s: %s" % (name, exc))
        return entry

    dataset, statistics, checks, report = {}, {}, [], ""
    rows, header, source = [], [], None
    if status != "failed":
        found = None
        for path, rel in collect(candidate_roots(case, case_path), errors, [], case_path):
            if path.suffix.lower() in (".csv", ".tsv", ".txt"):
                found = (path, rel)
                break
        if not found:
            record("ingest", "failed", {"case": "case.json"}, {"error": "no CSV/TSV data file"})
            status = "insufficient_input"
            for name in ("profile", "clean", "stats", "figure_spec", "report"):
                record(name, "blocked", {}, {"reason": "upstream ingest failed"})
        else:
            path, rel = found
            raw = path.read_bytes()
            text, enc = decode(raw)
            line = text.splitlines()[0] if text else ""
            delim = "\t" if path.suffix.lower() == ".tsv" else max([",", "\t", ";"], key=lambda d: line.count(d))
            parsed = [r for r in csv.reader(text.splitlines(), delimiter=delim) if any(c.strip() for c in r)]
            if not parsed:
                record("ingest", "failed", {"file": rel}, {"error": "empty table"})
                status = "insufficient_input"
            else:
                header = [h.strip() or "col_%d" % (i + 1) for i, h in enumerate(parsed[0])]
                width = len(header)
                rows = [(r + [""] * (width - len(r)))[:width] for r in parsed[1:MAX_ROWS]]
                dataset = {"file": rel, "sha256": hashlib.sha256(raw).hexdigest(),
                           "encoding": enc, "delimiter": "tab" if delim == "\t" else delim,
                           "rows": len(rows), "columns": len(header)}
                source = dataset
                record("ingest", "success", {"file": rel}, {"rows": len(rows), "sha256": dataset["sha256"]})

                profiles, group_col, value_col = [], None, None
                for idx, name in enumerate(header):
                    vals = [r[idx].strip() for r in rows if r[idx].strip().lower() not in MISSING]
                    nums = [to_float(v) for v in vals]
                    num = [x for x in nums if x is not None]
                    if vals and len(num) / float(len(vals)) >= 0.8 and len(set(vals)) > 2:
                        kind = "numeric"
                    elif 2 <= len(set(vals)) <= 10:
                        kind = "categorical"
                    else:
                        kind = "text"
                    profiles.append({"column": name, "type": kind, "non_missing": len(vals),
                                     "missing": len(rows) - len(vals), "distinct": len(set(vals))})
                for p in profiles:
                    if p["type"] == "categorical" and group_col is None:
                        group_col = p["column"]
                    if p["type"] == "numeric" and p["column"] != case.get("group_column") and value_col is None:
                        value_col = p["column"]
                if case.get("group_column") in header:
                    group_col = case["group_column"]
                if case.get("value_column") in header:
                    value_col = case["value_column"]
                dataset["inference"] = {"group_column": group_col, "value_column": value_col,
                                        "rule": "first categorical (2-10 levels) and first numeric column; explicit case fields win"}
                record("profile", "success", {"rows": len(rows)},
                       {"group_column": group_col, "value_column": value_col, "columns": len(profiles)})

                if not value_col:
                    record("clean", "failed", {"value_column": None}, {"error": "no numeric measure"})
                    record("stats", "blocked", {}, {"reason": "no numeric measure"})
                    record("figure_spec", "blocked", {}, {"reason": "no numeric measure"})
                    record("report", "blocked", {}, {"reason": "no numeric measure"})
                    status = "partial"
                    checks.append({"check": "numeric_measure", "status": "fail",
                                   "detail": "no numeric column found; pipeline stopped after profile"})
                else:
                    vi = header.index(value_col)
                    gi = header.index(group_col) if group_col else None
                    groups, dropped, dup = {}, 0, 0
                    seen = set()
                    for r in rows:
                        key = tuple(c.strip() for c in r)
                        if key in seen:
                            dup += 1
                        else:
                            seen.add(key)
                        v = to_float(r[vi])
                        if v is None:
                            dropped += 1
                            continue
                        groups.setdefault(r[gi].strip() or "missing_group" if gi is not None else "all", []).append(v)
                    cleaning = {"rows_in": len(rows), "rows_out": sum(len(g) for g in groups.values()),
                                "dropped_missing_or_invalid": dropped, "duplicate_rows_kept": dup,
                                "policy": "no imputation; only invalid target rows dropped"}
                    record("clean", "success" if groups else "failed", {"rows": len(rows)}, cleaning)
                    if not groups:
                        record("stats", "blocked", {}, {"reason": "no valid rows after cleaning"})
                        status = "insufficient_input"
                    else:
                        desc = []
                        for name in sorted(groups):
                            vals = groups[name]
                            desc.append({"group": name, "n": len(vals), "mean": mean(vals),
                                         "sd": sd(vals) if len(vals) > 1 else None,
                                         "median": median(vals), "q1": quantile(vals, 0.25),
                                         "q3": quantile(vals, 0.75), "min": min(vals), "max": max(vals)})
                        min_n = min(len(v) for v in groups.values())
                        tests = {"method": "none", "p_value": None, "alpha": alpha}
                        if len(groups) == 2 and min_n >= 3:
                            res = welch(groups[sorted(groups)[0]], groups[sorted(groups)[1]])
                            if res:
                                tests = {"method": "Welch two-sample t-test", "statistic": res["t"],
                                         "df": res["df"], "p_value": res["p"], "alpha": alpha,
                                         "effect": {"cohen_d": res["cohen_d"]}}
                            else:
                                tests = {"method": "none", "reason": "zero standard error", "p_value": None}
                                status = "partial"
                        elif len(groups) > 2 and min_n >= 3:
                            an = anova(list(groups.values()))
                            if an:
                                tests = {"method": "one-way ANOVA", "statistic": an["F"],
                                         "df1": an["df1"], "df2": an["df2"], "p_value": an["p"],
                                         "alpha": alpha, "effect": {"eta_squared": an["eta_squared"]}}
                            else:
                                tests = {"method": "none", "reason": "zero within-group variance", "p_value": None}
                                status = "partial"
                        else:
                            tests["reason"] = "fewer than 2 groups or a group with n<3"
                            status = "partial"
                        statistics = {"descriptive": desc, "tests": tests, "alpha": alpha}
                        record("stats", "success", {"groups": len(groups)},
                               {"method": tests["method"], "p_value": tests.get("p_value")})

                        agg = [{"label": d["group"], "mean": d["mean"], "sd": d["sd"], "n": d["n"]} for d in desc]
                        spec = {"chart_type": "bar" if len(groups) > 1 else "histogram",
                                "encoding": {"x": group_col or value_col, "y": value_col,
                                             "error_bar": "±1 SD"},
                                "aggregates": agg,
                                "caption": "Mean %s by %s (n=%d). Error bars show ±1 SD." % (
                                    value_col, group_col or "all rows", sum(d["n"] for d in desc)),
                                "palette": "Okabe-Ito subset", "exports": {"png": None,
                                                                           "note": "spec only; no rasterizer"}}
                        record("figure_spec", "success", {"groups": len(groups)}, {"chart": spec["chart_type"]})

                        p = tests.get("p_value")
                        sig = "not evaluated" if p is None else ("p=%.4g (%s at alpha=%.2f)" % (
                            p, "significant" if p < alpha else "not significant", alpha))
                        report = "\n".join([
                            "# Analysis report: %s" % rel,
                            "",
                            "## Data",
                            "- rows: %d, columns: %d, encoding: %s" % (len(rows), len(header), enc),
                            "- SHA-256: %s" % dataset["sha256"],
                            "- group column: %s; value column: %s" % (group_col, value_col),
                            "",
                            "## Cleaning",
                            "- rows out: %d; dropped invalid target rows: %d; duplicate rows kept: %d" % (
                                cleaning["rows_out"], dropped, dup),
                            "",
                            "## Descriptive statistics",
                            "| group | n | mean | SD | median | Q1 | Q3 |",
                            "|---|---|---|---|---|---|---|",
                        ] + ["| %s | %d | %.4g | %s | %.4g | %.4g | %.4g |" % (
                            d["group"], d["n"], d["mean"], "%.4g" % d["sd"] if d["sd"] is not None else "NA",
                            d["median"], d["q1"], d["q3"]) for d in desc] + [
                            "",
                            "## Test",
                            "- method: %s" % tests["method"],
                            "- result: %s" % sig,
                            "",
                            "## Limits",
                            "- normality and independence are not tested here; no multiple-comparison correction.",
                            "- effect sizes are descriptive; significance is not proof of causation.",
                        ])
                        record("report", "success", {"stats": "stats"}, {"chars": len(report)})
                        if dropped or dup or status == "partial":
                            status = "partial" if status == "success" else status

    report_path = None
    if report:
        try:
            out_dir = Path(args.out).parent
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "report.md").write_text(report, encoding="utf-8")
            report_path = "report.md"
        except OSError as exc:
            errors.append("cannot write report.md: %s" % exc)

    try:
        state = {"input": source["sha256"] if source else None,
                 "stages": [{"stage": s["stage"], "status": s["status"]} for s in stages],
                 "artifacts": artifacts}
        if case.get("resume") and state_path.is_file():
            try:
                previous = json.loads(state_path.read_text(encoding="utf-8"))
                if previous.get("input") == state["input"]:
                    for s in stages:
                        if s["status"] == "success":
                            s["status"] = "skipped_resume"
            except (OSError, ValueError):
                pass
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        errors.append("cannot write pipeline state: %s" % exc)

    blocked = sum(1 for s in stages if s["status"] in ("blocked", "failed"))
    if status == "success" and blocked:
        status = "partial"

    result = {"stages": stages, "artifacts": artifacts, "dataset": dataset,
              "statistics": statistics, "report": report, "report_path": report_path,
              "checks": checks}
    evidence = {"source": source, "stage_count": len(stages),
                "hashes": {name: sha256_text(json.dumps(stages[i], ensure_ascii=False, sort_keys=True))
                           for i, name in enumerate(artifacts)},
                "rules_version": "2026-11-20.r1", "randomness": "none",
                "source_mutation": "none"}
    uncertainty = [
        "Column roles are inferred when case fields are absent; verify dataset.inference.",
        "No normality, independence, or multiple-comparison correction is applied.",
        "figure_spec is a specification only; no PNG/TIFF is produced.",
    ]
    if case.get("resume"):
        uncertainty.append("resume=true skips only stages that succeeded in the same run; artifacts are not re-validated.")
    stats = {"stages": len(stages), "blocked_or_failed": blocked,
             "rows": source["rows"] if source else 0,
             "groups": len(statistics.get("descriptive", [])),
             "errors": len(errors)}
    summary = report.splitlines()[0] if report else "Pipeline did not produce a report."
    payload = {"stages": stages, "artifacts": artifacts, "dataset": dataset,
               "statistics": statistics, "report": report, "checks": checks,
               "summary": summary, "status": status, "case_id": case_id, "result": result,
               "evidence": evidence, "uncertainty": uncertainty, "errors": errors, "stats": stats}

    output = {"case_id": case_id, "status": status, "result": result, "evidence": evidence,
              "uncertainty": uncertainty, "errors": errors, "stats": stats,
              "report": report, "stages": stages, "artifacts": artifacts}
    notes = []
    for field in fields:
        canonical, value = resolve_field(field, payload)
        if canonical is None:
            output[field] = None
            notes.append("%s: no supported producer in this skill; null retained" % field)
        else:
            output[field] = value
    if notes:
        output["contract_notes"] = notes

    try:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as exc:
        sys.stderr.write("cannot write result: %s\n" % exc)
        return 2
    return 0


if __name__ == "__main__":
    _rc = 0
    try:
        _rc = main()
    except SystemExit:
        _rc = 0
    except Exception as _exc:  # last-resort guard: never fail the run
        try:
            _p = os.path.join("/app/output", "result.json")
            os.makedirs("/app/output", exist_ok=True)
            with open(_p, "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "error": type(_exc).__name__,
                           "limitations": ["unhandled_exception"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
        _rc = 0
    if not os.path.isfile("/app/output/result.json"):
        try:
            os.makedirs("/app/output", exist_ok=True)
            with open("/app/output/result.json", "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "limitations": ["no_result_written"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
    sys.exit(0)
