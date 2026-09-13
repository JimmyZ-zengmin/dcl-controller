#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_encoding.py -- scan (and optionally fix) non-UTF-8 text files.

WHY THIS EXISTS (2026-09-13):
  On this machine, non-ASCII content written through some paths lands as GBK
  (system ANSI) while other paths write UTF-8.  Mixing the two inside one file
  makes it undecodable by BOTH codecs -- a file that looks "corrupted" but is
  really just mixed-encoding.  Two wrong diagnoses were made that day
  ("concurrent write corruption", "5 KB size cap") before anyone checked the
  encoding.  So: check the encoding FIRST.

  This script is deliberately ASCII-only so its own encoding can never matter.

USAGE
  python tools/check_encoding.py <path> [<path> ...]
  python tools/check_encoding.py <path> --fix      # convert GBK -> UTF-8
  python tools/check_encoding.py . --fix --dry-run # report only

Exit code: 0 = all clean, 1 = problems found (usable as a gate).
"""
import sys

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import os

TEXT_EXT = {".c", ".h", ".md", ".py", ".sh", ".txt", ".cmake", ".ld", ".s",
            ".csv", ".json", ".yaml", ".yml", ".bat", ".html"}
SKIP_DIR = {"build", "build_min", "build_min485", ".git", "__pycache__",
            ".tmpctl", ".la", "node_modules", "tmp_dap"}


def classify(path):
    """Return (state, detail). state in: utf8 | gbk | mixed | binary"""
    try:
        b = open(path, "rb").read()
    except Exception as ex:
        return "binary", str(ex)
    if b"\x00" in b[:4096]:
        return "binary", "contains NUL"
    try:
        b.decode("utf-8")
        return "utf8", ""
    except UnicodeDecodeError:
        pass
    try:
        b.decode("gbk")
        return "gbk", ""
    except UnicodeDecodeError as e:
        return "mixed", str(e)


def walk(root):
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in TEXT_EXT:
                yield os.path.join(dirpath, fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--fix", action="store_true", help="rewrite GBK files as UTF-8")
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--quiet-utf8", action="store_true", help="do not list clean files")
    a = ap.parse_args()

    bad, fixed, n_utf8, n_bin = [], [], 0, 0
    for p in a.paths:
        for f in walk(p):
            st, detail = classify(f)
            if st == "utf8":
                n_utf8 += 1
                if not a.quiet_utf8:
                    print("  ok    UTF-8   %s" % f)
            elif st == "binary":
                n_bin += 1
            elif st == "gbk":
                bad.append(("gbk", f))
                print("  GBK   ******  %s" % f)
                if a.fix and not a.dry_run:
                    raw = open(f, "rb").read()
                    try:
                        open(f, "w", encoding="utf-8", newline="").write(
                            raw.decode("gbk"))
                        fixed.append(f)
                        print("        -> converted to UTF-8")
                    except Exception as ex:
                        print("        !! convert failed: %s" % ex)
            else:
                bad.append(("mixed", f))
                print("  MIXED ******  %s  (%s)" % (f, detail))

    print()
    print("summary: utf8=%d  binary(skipped)=%d  gbk=%d  mixed=%d  fixed=%d"
          % (n_utf8, n_bin,
             sum(1 for k, _ in bad if k == "gbk"),
             sum(1 for k, _ in bad if k == "mixed"),
             len(fixed)))
    if bad and not a.fix:
        print("hint: re-run with --fix to rewrite the GBK files as UTF-8.")
        print("      MIXED files cannot be auto-converted -- they need a manual")
        print("      decision (decode the parts you trust, rewrite the file).")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
