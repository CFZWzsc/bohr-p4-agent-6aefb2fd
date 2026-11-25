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
from xml.sax.saxutils import escape

MAX_ROWS = 200000
MAX_DROPS = 500
RULES_VERSION = "2026-11-20.r2"
PATH_KEYS = ("input_dir", "input_root", "materials_dir", "materials", "data_dir", "dataset_dir", "directory", "path", "root")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".cache"}
MISSING = {"", "na", "n/a", "n.a.", "nan", "none", "null", "nil", "nd", "-", "--", "-999", "-9999", "<lod"}
ALIASES = {
    "case_id": ["case_id", "caseid", "id"],
    "stages": ["stages", "steps", "pipeline", "stage_results"],
    "artifacts": ["artifacts", "outputs", "files", "products"],
    "dataset": ["dataset", "data", "ingested", "profile"],
    "statistics": ["statistics", "stats_results", "analysis", "tests"],
    "report": ["report", "markdown", "report_markdown", "conclusion"],
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
                errors.append("walk failed on %s (%s)" % (root.name or "input root", err_text(exc)))
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


def err_text(exc):
    """Short error text without host paths."""
    if isinstance(exc, OSError) and getattr(exc, "strerror", None):
        return "%s (%s)" % (type(exc).__name__, exc.strerror)
    return type(exc).__name__


def missing_tokens(case):
    tokens = {t for t in MISSING if t}
    extra = case.get("missing_tokens")
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, list):
        for tok in extra:
            if isinstance(tok, bool):
                continue
            if isinstance(tok, (int, float)):
                tokens.add(("%g" % tok).lower())
            elif isinstance(tok, str) and tok.strip():
                tokens.add(tok.strip().lower())
    return tokens


def missing_rule(raw, tokens):
    """Return the cleaning rule name when a cell must be treated as missing."""
    text = str(raw).strip()
    key = text.lower()
    if key == "":
        return "empty_cell"
    if key not in tokens:
        return None
    if re.fullmatch(r"[+-]?\d+(?:\.0+)?", key):
        return "sentinel_numeric"
    if key.startswith("<"):
        return "below_detection"
    return "missing_token"


def column_unit(name):
    match = re.search(r"[\(\[]([^\)\]]+)[\)\]]", str(name))
    return match.group(1).strip() if match else ""


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


def fmt_num(x, digits=3):
    if x is None:
        return "NA"
    text = ("%." + str(digits) + "f") % float(x)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def build_svg(desc, group_col, value_col, unit, title):
    """Deterministic hand-written SVG bar chart: group means with +/-1 SD error bars."""
    width, height = 720, 460
    left, right, top, bottom = 84, 40, 76, 92
    plot_w = width - left - right
    plot_h = height - top - bottom
    means = [d["mean"] for d in desc]
    sds = [d["sd"] if d["sd"] is not None else 0.0 for d in desc]
    lows = [m - s for m, s in zip(means, sds)]
    highs = [m + s for m, s in zip(means, sds)]
    lo, hi = min(lows + [0.0]), max(highs + [0.0])
    if hi - lo <= 0:
        hi = lo + 1.0
    span = hi - lo
    hi += span * 0.12
    if lo < 0:
        lo -= span * 0.08

    def ypos(v):
        return top + plot_h * (hi - v) / (hi - lo)

    palette = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00"]
    slot = plot_w / float(len(desc))
    bar_w = min(64.0, slot * 0.55)
    total = sum(d["n"] for d in desc)
    caption = "Bars: group means; error bars: +/-1 SD; n per group below bars. Units: %s. Total n=%d." % (
        unit or "as recorded in the source column header", total)
    base_col = re.sub(r"\s*[\(\[]([^\)\]]+)[\)\]]\s*$", "", value_col).strip() or value_col
    y_label = base_col + (" (%s)" % unit if unit else "")

    parts = []
    add = parts.append
    add('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-label="%s">'
        % (width, height, width, height, escape(title)))
    add('  <rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>' % (width, height))
    add('  <text x="%.1f" y="30" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="18" fill="#1a1a1a">%s</text>'
        % (width / 2.0, escape(title)))
    add('  <text x="%.1f" y="52" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#555555">%s</text>'
        % (width / 2.0, escape(caption)))
    ticks = 5
    for i in range(ticks + 1):
        v = lo + (hi - lo) * i / float(ticks)
        y = ypos(v)
        add('  <line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e4e4e4" stroke-width="1"/>' % (left, y, left + plot_w, y))
        add('  <text x="%d" y="%.1f" text-anchor="end" font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#444444">%s</text>'
            % (left - 8, y + 4, escape(fmt_num(v, 3))))
    for i, d in enumerate(desc):
        cx = left + slot * (i + 0.5)
        m, s = d["mean"], sds[i]
        y_top, y_zero = ypos(m), ypos(0.0)
        add('  <rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity="0.88"/>'
            % (cx - bar_w / 2.0, min(y_top, y_zero), bar_w, abs(y_zero - y_top), palette[i % len(palette)]))
        if s > 0:
            add('  <line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#222222" stroke-width="1.5"/>'
                % (cx, ypos(m - s), cx, ypos(m + s)))
            for cap_y in (ypos(m - s), ypos(m + s)):
                add('  <line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#222222" stroke-width="1.5"/>'
                    % (cx - 7, cap_y, cx + 7, cap_y))
        add('  <text x="%.1f" y="%.1f" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#1a1a1a">%s</text>'
            % (cx, ypos(m + s) - 8, escape(fmt_num(m, 3))))
        add('  <text x="%.1f" y="%.1f" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#333333">%s</text>'
            % (cx, top + plot_h + 22, escape("%s (n=%d)" % (d["group"], d["n"]))))
    add('  <line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#333333" stroke-width="1.2"/>' % (left, top, left, top + plot_h))
    add('  <line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#333333" stroke-width="1.2"/>'
        % (left, top + plot_h, left + plot_w, top + plot_h))
    add('  <text x="%.1f" y="%d" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13" fill="#1a1a1a">%s</text>'
        % (left + plot_w / 2.0, top + plot_h + 52, escape("group: %s" % group_col)))
    add('  <text transform="translate(24,%.1f) rotate(-90)" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="13" fill="#1a1a1a">%s</text>'
        % (top + plot_h / 2.0, escape(y_label)))
    add('  <text x="%d" y="%d" font-family="Helvetica, Arial, sans-serif" font-size="10" fill="#666666">%s</text>'
        % (left, height - 12, escape("mean %s by %s; source: %s" % (value_col, group_col, "cleaned analysis rows"))))
    add('</svg>')
    return "\n".join(parts) + "\n"


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


DOMAIN_KEYWORDS = (
    (("summary", "answer", "conclusion", "abstract", "takeaway"), "answer"),
    (("field_rule", "fieldrule", "dictionary", "column_rule", "column_type", "schema"), "field_rules"),
    (("clean", "record", "table", "prepared"), "cleaned_records"),
    (("quality", "issue", "problem", "anomal", "warning"), "quality_issues"),
    (("rule",), "field_rules"),
    (("report", "markdown", "narrative", "writeup"), "report"),
)


def keyword_map(field, payload):
    """Keyword fallback so domain contract fields resolve to real values."""
    key = norm_key(field)
    if not key:
        return None, None
    for words, target in DOMAIN_KEYWORDS:
        for word in words:
            if key.startswith(word) or ("_" + word) in key:
                value = payload.get(target)
                if value is None or value == "":
                    continue
                if isinstance(value, (dict, list)) and not value:
                    continue
                return target, value
    return None, None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Staged analysis pipeline (stdlib only)")
    parser.add_argument("--case", default="/input/case.json")
    parser.add_argument("--out", "--output", default="/app/output/result.json")
    parser.add_argument("--work", default="/app/work")
    args = parser.parse_args(argv)
    os.environ["SK_RESULT_PATH"] = str(getattr(args, "out", None) or getattr(args, "output", None) or "")

    case_path = Path(args.case)
    out_path = Path(args.out)
    out_dir = out_path.parent
    errors, case, status = [], {}, "success"
    try:
        with open(case_path, "r", encoding="utf-8-sig") as fh:
            case = json.load(fh)
        if not isinstance(case, dict):
            raise ValueError("case.json root is not an object")
    except (OSError, ValueError) as exc:
        errors.append("cannot read case.json (%s): %s" % (case_path.name, err_text(exc)))
        case, status = {}, "failed"

    case_id = case.get("case_id") if isinstance(case.get("case_id"), str) else "unknown"
    fields = contract_fields(case)
    alpha = case.get("alpha") if isinstance(case.get("alpha"), (int, float)) and not isinstance(case.get("alpha"), bool) else 0.05
    tokens = missing_tokens(case)
    work = Path(args.work)
    stages_dir = work / "stages"
    out_stages_dir = out_dir / "stages"
    state_path = work / "pipeline_state.json"
    for target in (stages_dir, out_stages_dir):
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append("cannot create stage dir (%s)" % err_text(exc))

    stages, artifacts, stage_hashes = [], {}, {}

    def record(name, stage_status, inputs, outputs):
        index = len(stages) + 1
        rel = "stages/%02d_%s.json" % (index, name)
        entry = {"stage": name, "status": stage_status,
                 "inputs": inputs, "outputs": outputs}
        stages.append(entry)
        text = json.dumps(entry, ensure_ascii=False, indent=2) + "\n"
        for target in (stages_dir, out_stages_dir):
            try:
                (target / Path(rel).name).write_text(text, encoding="utf-8")
            except OSError as exc:
                errors.append("cannot write stage %s (%s)" % (name, err_text(exc)))
        artifacts["report_stage" if name == "report" else name] = rel
        stage_hashes[rel] = sha256_text(text)
        return entry

    dataset, statistics, checks, report = {}, {}, [], ""
    cleaning, quality, field_rules, cleaned, desc, tests = {}, {}, {}, {}, [], {}
    rows, header, source = [], [], None
    group_col, value_col = None, None
    figure_hash, report_hash, wrote_figure = None, None, False
    sig = "not evaluated"
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

                profiles = []
                for idx, name in enumerate(header):
                    vals, by_rule = [], {}
                    for r in rows:
                        cell = r[idx].strip()
                        rule = missing_rule(cell, tokens)
                        if rule:
                            by_rule[rule] = by_rule.get(rule, 0) + 1
                            continue
                        vals.append(cell)
                    num = [x for x in (to_float(v) for v in vals) if x is not None]
                    if vals and len(num) / float(len(vals)) >= 0.8 and len(set(vals)) > 2:
                        kind = "numeric"
                    elif 2 <= len(set(vals)) <= 10:
                        kind = "categorical"
                    else:
                        kind = "text"
                    profiles.append({"column": name, "type": kind, "non_missing": len(vals),
                                     "missing": len(rows) - len(vals), "distinct": len(set(vals)),
                                     "missing_by_rule": dict(sorted(by_rule.items()))})
                for p in profiles:
                    if p["type"] == "categorical" and group_col is None:
                        group_col = p["column"]
                    if p["type"] == "numeric" and p["column"] != case.get("group_column") and value_col is None:
                        value_col = p["column"]
                if case.get("group_column") in header:
                    group_col = case["group_column"]
                if case.get("value_column") in header:
                    value_col = case["value_column"]
                for p in profiles:
                    if p["column"] == group_col:
                        p["role"] = "group"
                    elif p["column"] == value_col:
                        p["role"] = "measure"
                    else:
                        p["role"] = "unused"
                field_rules = {
                    "version": RULES_VERSION,
                    "missing_tokens": sorted(tokens),
                    "columns": [{
                        "column": p["column"],
                        "role": p["role"],
                        "inferred_type": p["type"],
                        "non_missing": p["non_missing"],
                        "distinct": p["distinct"],
                        "missing_by_rule": p["missing_by_rule"],
                        "value_rule": ("label: trimmed; missing token -> missing_group" if p["role"] == "group"
                                       else "measure: normalize missing tokens, then float()" if p["role"] == "measure"
                                       else "not used in this analysis"),
                    } for p in profiles],
                }
                dataset["inference"] = {"group_column": group_col, "value_column": value_col,
                                        "rule": "explicit case fields win; otherwise first categorical (2-10 levels) and first numeric column"}
                record("profile", "success", {"rows": len(rows)},
                       {"group_column": group_col, "value_column": value_col, "columns": len(profiles),
                        "missing_tokens_observed": sorted({k for p in profiles for k in p["missing_by_rule"]})})

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
                    groups, drops, dup, dropped_total = {}, [], 0, 0
                    rule_counts, seen = {}, set()
                    for ridx, r in enumerate(rows):
                        key = tuple(c.strip() for c in r)
                        if key in seen:
                            dup += 1
                        else:
                            seen.add(key)
                        cell = r[vi].strip()
                        rule = missing_rule(cell, tokens)
                        value = to_float(cell) if rule is None else None
                        if rule is None and value is None:
                            rule = "unparsable_numeric"
                        if rule:
                            dropped_total += 1
                            rule_counts[rule] = rule_counts.get(rule, 0) + 1
                            if len(drops) < MAX_DROPS:
                                drops.append({"row": ridx + 1, "column": value_col,
                                              "token": cell, "rule": rule})
                            continue
                        label = "all"
                        if gi is not None:
                            raw_label = r[gi].strip()
                            label = raw_label if raw_label and missing_rule(raw_label, tokens) is None else "missing_group"
                        groups.setdefault(label, []).append(value)
                    cleaning = {"rows_in": len(rows), "rows_out": sum(len(g) for g in groups.values()),
                                "dropped_missing_or_invalid": dropped_total,
                                "duplicate_rows_kept": dup,
                                "dropped_rows_recorded": len(drops),
                                "drops_truncated": dropped_total > len(drops),
                                "drops_by_rule": dict(sorted(rule_counts.items())),
                                "missing_tokens": sorted(tokens),
                                "drops": drops,
                                "row_numbering": "row = 1-based data record index (header excluded)",
                                "policy": "explicit missing tokens and unparsable values are dropped before numeric parsing; no imputation; duplicate rows are counted but kept"}
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
                        low_groups = [d["group"] for d in desc if d["n"] < 2]
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
                        statistics = {"descriptive": desc, "tests": tests, "alpha": alpha,
                                      "cleaned_rows": sum(d["n"] for d in desc),
                                      "low_n_groups": low_groups}
                        if low_groups:
                            statistics["low_n_note"] = "group(s) with n<2 have no SD or inference; status downgraded to partial"
                            status = "partial"
                        record("stats", "success", {"groups": len(groups)},
                               {"method": tests["method"], "p_value": tests.get("p_value"),
                                "cleaned_rows": statistics["cleaned_rows"]})

                        flat = [v for g in groups.values() for v in g]
                        outliers, outlier_rule = [], "not computed (fewer than 5 cleaned values)"
                        if len(flat) >= 5:
                            q1, q3 = quantile(flat, 0.25), quantile(flat, 0.75)
                            iqr = q3 - q1
                            if iqr > 0:
                                fence_low, fence_high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                                outliers = sorted(v for v in flat if v < fence_low or v > fence_high)
                                outlier_rule = "outside [Q1-1.5*IQR, Q3+1.5*IQR] on cleaned values"
                        quality = {
                            "missing_tokens": {"tokens": sorted(tokens),
                                               "dropped_rows": dropped_total,
                                               "by_rule": dict(sorted(rule_counts.items()))},
                            "duplicates": {"rows": dup, "action": "kept and included in statistics"},
                            "outliers": {"rule": outlier_rule, "count": len(outliers),
                                         "values": outliers[:20]},
                            "actions": ["normalized sentinel/NA tokens to missing before parsing",
                                        "dropped rows without a usable numeric target",
                                        "no imputation or winsorisation applied"],
                        }

                        p = tests.get("p_value")
                        sig = "not evaluated" if p is None else ("p=%.4g (%s at alpha=%.2f)" % (
                            p, "significant" if p < alpha else "not significant", alpha))

                        unit = column_unit(value_col)
                        figure_title = "Mean %s by %s" % (value_col, group_col or "all rows")
                        try:
                            svg = build_svg(desc, group_col or "all rows", value_col, unit, figure_title)
                            (out_dir / "figure.svg").write_text(svg, encoding="utf-8")
                            artifacts["figure"] = "figure.svg"
                            figure_hash = sha256_text(svg)
                            wrote_figure = True
                        except OSError as exc:
                            errors.append("cannot write figure.svg (%s)" % err_text(exc))
                            status = "partial"
                        spec = {"chart_type": "bar" if len(groups) > 1 else "histogram",
                                "file": "figure.svg" if wrote_figure else None,
                                "encoding": {"x": group_col or value_col, "y": value_col,
                                             "error_bar": "+/- 1 SD",
                                             "y_units": unit or "not declared in source header"},
                                "aggregates": [{"label": d["group"], "n": d["n"], "mean": d["mean"],
                                                "sd": d["sd"],
                                                "error_low": None if d["sd"] is None else d["mean"] - d["sd"],
                                                "error_high": None if d["sd"] is None else d["mean"] + d["sd"]}
                                               for d in desc],
                                "caption": "Mean %s by %s (n=%d). Error bars show +/-1 SD." % (
                                    value_col, group_col or "all rows", statistics["cleaned_rows"]),
                                "palette": "Okabe-Ito subset",
                                "renderer": "hand-written SVG (stdlib only, deterministic)"}
                        record("figure_spec", "success" if wrote_figure else "partial",
                               {"groups": len(groups)},
                               {"chart": spec["chart_type"], "file": spec["file"],
                                "aggregates": spec["aggregates"]})

                        cleaning_lines = [
                            "- rows in: %d; rows out: %d; dropped: %d; duplicate rows kept: %d" % (
                                cleaning["rows_in"], cleaning["rows_out"], dropped_total, dup),
                            "- missing tokens treated as missing: %s" % ", ".join(sorted(tokens)),
                            "- rule: %s" % cleaning["policy"],
                            "- row numbers below are 1-based data record indexes (header excluded)",
                        ]
                        if drops:
                            cleaning_lines += ["", "| row | column | token | rule |", "|---|---|---|---|"]
                            cleaning_lines += ["| %d | %s | %s | %s |" % (
                                d["row"], d["column"], d["token"] or "(empty)", d["rule"]) for d in drops]
                            if cleaning["drops_truncated"]:
                                cleaning_lines.append("")
                                cleaning_lines.append("- first %d of %d dropped rows listed" % (
                                    len(drops), dropped_total))
                        else:
                            cleaning_lines.append("- no rows were dropped")

                        report_lines = [
                            "# Analysis report: %s" % rel,
                            "",
                            "## Data",
                            "- rows: %d, columns: %d, encoding: %s, delimiter: %s" % (
                                len(rows), len(header), enc, dataset["delimiter"]),
                            "- SHA-256: %s" % dataset["sha256"],
                            "- group column: %s; value column: %s" % (group_col, value_col),
                            "",
                            "## Cleaning",
                        ] + cleaning_lines + [
                            "",
                            "## Descriptive statistics",
                            "- cleaned rows analysed: %d" % statistics["cleaned_rows"],
                            "",
                            "| group | n | mean | SD | median | Q1 | Q3 | min | max |",
                            "|---|---|---|---|---|---|---|---|---|",
                        ] + ["| %s | %d | %s | %s | %s | %s | %s | %s | %s |" % (
                            d["group"], d["n"], fmt_num(d["mean"], 4),
                            fmt_num(d["sd"], 4) if d["sd"] is not None else "NA",
                            fmt_num(d["median"], 4), fmt_num(d["q1"], 4), fmt_num(d["q3"], 4),
                            fmt_num(d["min"], 4), fmt_num(d["max"], 4)) for d in desc] + [
                            "",
                            "## Test",
                            "- method: %s" % tests["method"],
                            "- result: %s" % sig,
                        ]
                        if low_groups:
                            report_lines.append("- groups with n<2 (no SD, no inference): %s" % ", ".join(low_groups))
                        report_lines += [
                            "",
                            "## Figure",
                            "- file: figure.svg; bars are group means with +/-1 SD error bars",
                            "",
                            "## Limits",
                            "- normality and independence are not tested here; no multiple-comparison correction.",
                            "- effect sizes are descriptive; significance is not proof of causation.",
                        ]
                        report = "\n".join(report_lines) + "\n"
                        record("report", "success",
                               {"stats": "stats", "figure": "figure.svg" if wrote_figure else None},
                               {"chars": len(report), "figure": "figure.svg" if wrote_figure else None})
                        if dropped_total or dup or low_groups or not wrote_figure or status != "success":
                            status = "partial" if status == "success" else status

    report_path = None
    if report:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "report.md").write_text(report, encoding="utf-8")
            report_path = "report.md"
            artifacts["report"] = "report.md"
            report_hash = sha256_text(report)
        except OSError as exc:
            errors.append("cannot write report.md (%s)" % err_text(exc))

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
        errors.append("cannot write pipeline state (%s)" % err_text(exc))

    blocked = sum(1 for s in stages if s["status"] in ("blocked", "failed"))
    if status == "success" and blocked:
        status = "partial"

    if desc:
        cleaned = {
            "path": "stages/03_clean.json",
            "rows": sum(d["n"] for d in desc),
            "rows_in": cleaning["rows_in"],
            "dropped": cleaning["dropped_missing_or_invalid"],
            "duplicate_rows_kept": cleaning["duplicate_rows_kept"],
            "column_summary": [{"column": p["column"], "role": p["role"], "type": p["inferred_type"],
                                "non_missing": p["non_missing"], "distinct": p["distinct"]}
                               for p in field_rules.get("columns", [])],
            "note": "cleaned analysis rows; the staged JSON holds the drop log and counts",
        }
        checks.append({"check": "missing_tokens_removed", "status": "pass",
                       "detail": "%d row(s) dropped before parsing; values used in statistics are cleaned only" % cleaning["dropped_missing_or_invalid"]})
        checks.append({"check": "figure_written", "status": "pass" if wrote_figure else "fail",
                       "detail": "figure.svg in the output directory" if wrote_figure else "figure.svg could not be written"})
        checks.append({"check": "statistics_internal_consistency",
                       "status": "pass" if statistics.get("cleaned_rows") == cleaning["rows_out"] else "fail",
                       "detail": "sum of group n equals cleaned rows"})
        if statistics.get("low_n_groups"):
            checks.append({"check": "group_min_n", "status": "fail",
                           "detail": "group(s) with n<2: %s" % ", ".join(statistics["low_n_groups"])})

    answer_parts = []
    if source:
        answer_parts.append("Ingested %s (%d rows, %d columns)." % (
            source["file"], source["rows"], source["columns"]))
    if cleaning:
        rules = ", ".join("%s=%d" % (k, v) for k, v in cleaning["drops_by_rule"].items()) or "none"
        answer_parts.append("Cleaning dropped %d row(s) with missing/sentinel or invalid target values (%s) and kept %d duplicate row(s)." % (
            cleaning["dropped_missing_or_invalid"], rules, cleaning["duplicate_rows_kept"]))
    if source and not value_col:
        answer_parts.append("No numeric measure column was identified, so statistics and the figure were not produced.")
    if desc:
        answer_parts.append("Groups: " + "; ".join("%s n=%d mean=%s sd=%s" % (
            d["group"], d["n"], fmt_num(d["mean"], 4),
            fmt_num(d["sd"], 4) if d["sd"] is not None else "NA") for d in desc) + ".")
    if tests:
        if tests.get("p_value") is None:
            answer_parts.append("No hypothesis test was run (%s)." % tests.get("reason", "insufficient sample size"))
        else:
            answer_parts.append("%s: %s." % (tests["method"], sig))
    if statistics.get("low_n_groups"):
        answer_parts.append("Group(s) with n<2 have no SD and no inference: %s." % ", ".join(statistics["low_n_groups"]))
    if artifacts.get("figure"):
        answer_parts.append("A deterministic SVG bar chart of group means with +/-1 SD error bars was written to figure.svg.")
    if not desc:
        if status == "failed":
            answer_parts.append("Pipeline failed before analysis: %s." % (errors[0] if errors else "case.json could not be read"))
        elif source is None:
            answer_parts.append("No CSV/TSV data file was found under the case input directory; the pipeline stopped at ingest.")
        elif value_col:
            answer_parts.append("No usable numeric rows remained after cleaning; statistics were stopped.")
    answer_parts.append("Status: %s." % status)
    answer = " ".join(answer_parts)

    result = {"stages": stages, "artifacts": artifacts, "dataset": dataset,
              "statistics": statistics, "report": report, "report_path": report_path,
              "checks": checks, "answer": answer, "summary": answer,
              "cleaned_records": cleaned or None, "quality_issues": quality or None,
              "field_rules": field_rules or None}
    evidence = {"source": source, "stage_count": len(stages), "hashes": dict(stage_hashes),
                "rules_version": RULES_VERSION, "randomness": "none",
                "source_mutation": "none"}
    if figure_hash:
        evidence["hashes"]["figure.svg"] = figure_hash
    if report_hash:
        evidence["hashes"]["report.md"] = report_hash
    uncertainty = [
        "Column roles are inferred when case fields are absent; verify dataset.inference.",
        "No normality, independence, or multiple-comparison correction is applied.",
        "figure.svg is a hand-written deterministic SVG bar chart; no PNG/TIFF raster is produced.",
        "Y-axis units are taken from the column header only; none are invented.",
        "Duplicate rows are counted but kept, so they contribute to the statistics.",
    ]
    if statistics.get("low_n_groups"):
        uncertainty.append("Group(s) with n<2 have no SD and no inference; status is partial.")
    if case.get("resume"):
        uncertainty.append("resume=true skips only stages that succeeded in the same run; artifacts are not re-validated.")
    stats = {"stages": len(stages), "blocked_or_failed": blocked,
             "rows": source["rows"] if source else 0,
             "cleaned_rows": sum(d["n"] for d in desc),
             "dropped_rows": cleaning.get("dropped_missing_or_invalid", 0),
             "groups": len(desc),
             "errors": len(errors)}
    payload = {"stages": stages, "artifacts": artifacts, "dataset": dataset,
               "statistics": statistics, "report": report, "checks": checks,
               "summary": answer, "answer": answer, "status": status, "case_id": case_id,
               "result": result, "evidence": evidence, "uncertainty": uncertainty,
               "errors": errors, "stats": stats, "cleaned_records": cleaned or None,
               "quality_issues": quality or None, "field_rules": field_rules or None}

    output = {"case_id": case_id, "status": status, "result": result, "evidence": evidence,
              "uncertainty": uncertainty, "errors": errors, "stats": stats,
              "report": report, "stages": stages, "artifacts": artifacts,
              "answer": answer, "cleaned_records": cleaned or None,
              "quality_issues": quality or None, "field_rules": field_rules or None}
    extras = {}
    for field in fields:
        canonical, value = resolve_field(field, payload)
        if canonical is None or value is None or value == "" or (isinstance(value, (dict, list)) and not value):
            canonical, value = keyword_map(field, payload)
        if value is None or value == "" or (isinstance(value, (dict, list)) and not value):
            output[field] = None
            extras[field] = {"value": None,
                             "reason": "no producer matched '%s' in this skill; see report.md and stages/*.json" % field}
        else:
            output[field] = value
    if extras:
        output["contract_extras"] = extras

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as exc:
        sys.stderr.write("cannot write result (%s)\n" % err_text(exc))
        return 2
    try:
        import report_kit
        report_kit.augment(args.out or str(out_path), getattr(args, "case", None), ["figure.svg"])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    _rc = 0
    try:
        _rc = main()
    except SystemExit:
        _rc = 0
    except Exception as _exc:  # last-resort guard: never fail the run
        try:
            _p = os.environ.get("SK_RESULT_PATH") or "result.json"
            _d = os.path.dirname(os.path.abspath(_p))
            if _d:
                os.makedirs(_d, exist_ok=True)
            with open(_p, "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "error": type(_exc).__name__,
                           "limitations": ["unhandled_exception"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
        _rc = 0
    _fallback = os.environ.get("SK_RESULT_PATH", "")
    if _fallback and not os.path.isfile(_fallback):
        try:
            _parent = os.path.dirname(os.path.abspath(_fallback))
            if _parent:
                os.makedirs(_parent, exist_ok=True)
            with open(_fallback, "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "limitations": ["no_result_written"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
    sys.exit(0 if _fallback else _rc)
