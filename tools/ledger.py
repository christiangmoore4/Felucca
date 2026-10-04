#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Felucca fork contributors
"""Static resource ledger: what the firmware's RAM, POOL, NOINIT and constant data cost,
worked out from source with no pi32v2 toolchain, no SDK and no hardware.

  tools/ledger.py                  print the ledger
  tools/ledger.py --check          compare with tests/ledger_baseline.txt (BUDGET_UPDATE=1 rewrites it)
  tools/ledger.py --json FILE      also write the numbers as JSON
  tools/ledger.py --top N          largest objects per region (default 5, 0 = none)
  tools/ledger.py --samples        per-sample table of the factory sets

How: firmware/src/felucca.c is compiled to assembly (-S) with a 32-bit host compiler and the
app's own flags (build.py app_flags(), so the FELUCCA_* environment flags count). The pi32v2
inline asm in hal/ is only text to -S. The size of every object is read from its .size
directive and added up by linker region: RAM (.data + .bss), POOL (.pool), NOINIT (.noinit)
and constants (.rodata). Capacities are read from firmware/app.ld, the pool reserve is
build.py's POOL_RESERVE, the factory sample sizes come from build/gen/felucca_samples.h.

What it is not: a target measurement. Data sizes do not depend on the ISA (both are ILP32),
but the linker adds alignment padding that this does not (under 1 %), and nothing here knows
the code size or the stack depth: those need the target build (the PDF's M0b). The same
numbers come out of the real link map in build.py check(); compare them on the first target
build and keep the difference in the baseline's header.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
FW = SRC / "firmware"
GEN = SRC / "build" / "gen"
BASELINE = SRC / "tests" / "ledger_baseline.txt"
TOL = 0.01                       # growth over the baseline that fails --check (a few bytes of slack too)
SLACK = 64
sys.path.insert(0, str(SRC / "tools"))
import build  # noqa: E402  (app_flags(), generate(), POOL_RESERVE; importing it has no side effects)

HOST_FLAGS = ["-S", "-w", "-ffreestanding", "-fno-pic", "-fno-pie", "-fno-stack-protector",
              "-fcf-protection=none", "-fno-asynchronous-unwind-tables"]
CANDIDATES = [["gcc", "-m32"], ["clang", "--target=i386-unknown-linux-gnu", "-fno-integrated-as"]]   # clang must not assemble the pi32v2 asm
NO_CC = 3                        # exit status: no suitable compiler (tests/run_tests.sh may skip, CI must not)
SETUP = "tools/build.py --gen-only"


def find_cc():
    """a compiler that turns freestanding C into 32-bit assembly: $LEDGER_CC, else gcc -m32, else clang"""
    env = os.environ.get("LEDGER_CC")
    for cc in ([shlex.split(env)] if env else CANDIDATES):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "t.c"
            t.write_text("#include <stdint.h>\nstatic uint8_t x[3];\nuint8_t f(void) { return x[1]; }\n")
            try:
                r = subprocess.run([*cc, *HOST_FLAGS, "-o", str(Path(d) / "t.s"), str(t)], capture_output=True)
            except OSError:
                continue
            if r.returncode == 0:
                return cc
    print("ledger: no 32-bit capable C compiler (tried " + (env or ", ".join(" ".join(c) for c in CANDIDATES))
          + "); set LEDGER_CC", file=sys.stderr)
    sys.exit(NO_CC)


def compile_asm():
    for h in ("felucca_font.h", "felucca_icons.h", "felucca_tables.h", "felucca_samples.h"):
        if not (GEN / h).exists():
            print(f"ledger: {h} missing; running {SETUP}", file=sys.stderr)
            build.generate()
            break
    cc = find_cc()
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "felucca.s"
        flags = [f for f in build.app_flags() if f not in ("-Wall", "-Wno-unused-function")]
        r = subprocess.run([*cc, *flags, *HOST_FLAGS, "-o", str(out), str(FW / "src" / "felucca.c")],
                           cwd=SRC, capture_output=True, text=True)
        if r.returncode:
            sys.stderr.write("\n".join(r.stderr.splitlines()[:12]) + "\n")
            raise SystemExit("ledger: the host compile of firmware/src/felucca.c failed")
        return out.read_text(), " ".join(cc)


def bucket(sec):
    if sec.startswith(".pool"):
        return "pool"
    if sec.startswith(".noinit"):
        return "noinit"
    if sec.startswith((".rodata", ".data.rel.ro")):
        return "rodata"
    if sec.startswith((".bss", ".sbss", ".data", ".sdata")):
        return "ram"
    return None                  # code, unwind tables, notes


def c_len(lit):
    """bytes of a C string literal body (gcc and clang write octal / simple escapes)"""
    n, i = 0, 0
    while i < len(lit):
        if lit[i] == "\\" and i + 1 < len(lit):
            i += 1
            if lit[i] in "01234567":
                j = i
                while j < len(lit) and j < i + 3 and lit[j] in "01234567":
                    j += 1
                i = j - 1
            elif lit[i] == "x":
                j = i + 1
                while j < len(lit) and lit[j] in "0123456789abcdefABCDEF":
                    j += 1
                i = j - 1
        n += 1
        i += 1
    return n


def parse_asm(text):
    """-> {bucket: [(bytes, name)]}, string literal bytes"""
    objs = {b: [] for b in ("ram", "pool", "noinit", "rodata")}
    strings = 0
    sec = ".text"
    for ln in text.splitlines():
        s = ln.strip()
        m = re.match(r"\.section\s+([^\s,]+)", s)
        if m:
            sec = m.group(1)
            continue
        if s in (".text", ".data", ".bss", ".rodata"):
            sec = s
            continue
        m = re.match(r"\.(?:comm|lcomm)\s+([\w.$]+),\s*(\d+)", s)
        if m:
            objs["ram"].append((int(m.group(2)), m.group(1)))
            continue
        m = re.match(r"\.size\s+([\w.$]+),\s*(\d+)$", s)
        if m:
            b = bucket(sec)
            if b and not sec.startswith(".rodata.str"):
                objs[b].append((int(m.group(2)), m.group(1)))
            continue
        if sec.startswith(".rodata.str"):
            m = re.match(r'\.(string|asciz|ascii)\s+"(.*)"\s*$', s)
            if m:
                strings += c_len(m.group(2)) + (0 if m.group(1) == "ascii" else 1)
    return objs, strings


def app_ld():
    """capacities from firmware/app.ld: MEMORY regions and the stack windows"""
    t = (FW / "app.ld").read_text()
    reg = {}
    for m in re.finditer(r"^\s*(\w+)\s*\([rwx]+\)\s*:\s*ORIGIN\s*=\s*(0x[0-9A-Fa-f]+)\s*,\s*LENGTH\s*=\s*(0x[0-9A-Fa-f]+|\d+)(K?)",
                         t, re.M):
        v = int(m.group(3), 0) * (1024 if m.group(4) else 1)
        reg[m.group(1)] = v
    sym = {m.group(1): int(m.group(2), 16) for m in re.finditer(r"\b(_\w+)\s*=\s*0x([0-9A-Fa-f]+)\s*;", t)}
    stacks = {"user": sym["_ustack_top"] - sym["_ustack_lo"], "supervisor": sym["_sstack_top"] - sym["_sstack_lo"]}
    return reg, stacks


def samples():
    """factory sample sets from the generated header: {set: (zones, unique, bytes, seconds)}, SMP_DATA, per-sample rows"""
    t = (GEN / "felucca_samples.h").read_text()
    total = int(re.search(r"SMP_DATA\[(\d+)\]", t).group(1))
    zones = [[int(x) for x in re.findall(r"-?\d+", ln)]
             for ln in re.search(r"SMP_ZONES\[\] = \{(.*?)\n\};", t, re.S).group(1).strip().splitlines()]
    sets = re.findall(r'\{"(\w+)", (\d+), (\d+)\},', re.search(r"SMP_SETS\[\] = \{(.*?)\n\};", t, re.S).group(1))
    out, rows = {}, {}
    for name, z0, nz in sets:
        z = zones[int(z0):int(z0) + int(nz)]
        uniq = {e[0]: e for e in z}                 # zones of one set share a sample by offset
        nbytes = sum((e[1] + 1) // 2 + (((e[1] + 1) // 2) & 1) for e in uniq.values())
        secs = sum(e[1] / (e[4] / 65536 * 44100) for e in uniq.values())
        out[name] = (len(z), len(uniq), nbytes, secs)
        rows[name] = sorted(((e[1] + 1) // 2 + (((e[1] + 1) // 2) & 1), e[1] / (e[4] / 65536 * 44100),
                             sorted({(x[8], x[9]) for x in z if x[0] == off}))
                            for off, e in uniq.items())
    return out, total, rows


def collect():
    text, cc = compile_asm()
    objs, strings = parse_asm(text)
    reg, stacks = app_ld()
    sets, smp_total, rows = samples()
    m = {k: sum(n for n, _ in v) for k, v in objs.items()}
    m["strings"] = strings
    m["smp_data"] = smp_total
    for name, (_, _, b, _) in sets.items():
        m["set/" + name] = b
    cap = {"ram": reg["RAM"], "pool": reg["POOL"], "noinit": reg["NOINIT"], "xip": reg["XIP"]}
    return m, cap, stacks, objs, sets, rows, cc


def fmt(n):
    return f"{n:,}"


def report(m, cap, stacks, objs, sets, rows, cc, top, per_sample):
    print(f"Felucca static resource ledger  ({cc}, firmware/src/felucca.c; data sizes, not target bytes)")
    print(f"{'region':8} {'used':>9} {'capacity':>9} {'spare':>9}  note")
    pool_spare = cap["pool"] - m["pool"]
    for key, label, note in (("ram", "RAM", ".data + .bss (the check in build.py is the same sum)"),
                             ("pool", "POOL", f"{fmt(pool_spare - build.POOL_RESERVE)} after build.py's {fmt(build.POOL_RESERVE)} B reserve"),
                             ("noinit", "NOINIT", "survives resets: project slots, boot guard, debug")):
        print(f"{label:8} {fmt(m[key]):>9} {fmt(cap[key]):>9} {fmt(cap[key] - m[key]):>9}  {note}")
    const = m["rodata"] + m["strings"]
    print(f"{'CONST':8} {fmt(const):>9} {fmt(cap['xip']):>9} {fmt(cap['xip'] - const):>9}  "
          f".rodata + strings of the app slot ({const * 100 / cap['xip']:.1f} %); code is not included (needs the target build)")
    print(f"stacks   user {fmt(stacks['user'])} B, supervisor {fmt(stacks['supervisor'])} B; depth is not measurable here")
    print()
    print(f"factory samples (build/gen/felucca_samples.h): SMP_DATA {fmt(m['smp_data'])} B = "
          f"{m['smp_data'] * 100 / cap['xip']:.1f} % of the app slot")
    for name, (nz, nu, b, secs) in sets.items():
        print(f"  {name:6} {nz:3} zones {nu:3} samples {fmt(b):>8} B {secs:7.2f} s")
    if sum(b for _, _, b, _ in sets.values()) != m["smp_data"]:
        print("  warning: the sets do not add up to SMP_DATA (shared samples across sets?)")
    if per_sample:
        for name, r in rows.items():
            print(f"\n{name}: bytes, seconds, GM key ranges")
            for b, secs, keys in sorted(r, reverse=True):
                print(f"  {fmt(b):>8} B {secs:6.3f} s  {keys}")
    if top:
        for key, label in (("pool", "POOL"), ("ram", "RAM"), ("noinit", "NOINIT"), ("rodata", "CONST")):
            print(f"\nlargest in {label}:")
            for n, name in sorted(objs[key], reverse=True)[:top]:
                print(f"  {fmt(n):>9}  {name}")


TRACKED = ("pool", "ram", "noinit", "rodata", "strings", "smp_data")


def read_baseline():
    base = {}
    for ln in BASELINE.read_text().splitlines():
        if ln.strip() and not ln.startswith("#"):
            k, v = ln.split()
            base[k] = int(v)
    return base


def write_baseline(m, cc):
    keys = [*TRACKED, *sorted(k for k in m if k.startswith("set/"))]
    BASELINE.write_text(
        "# FELUCCA static resource ledger baseline (tools/ledger.py): bytes of the objects in each region,\n"
        f"# from a 32-bit host compile of firmware/src/felucca.c ({cc}). Not target bytes: the linker adds\n"
        "# padding (under 1 %) and code size / stack depth are not here. The check allows +1 % growth; a\n"
        "# smaller number is noted, not failed. Rewritten by BUDGET_UPDATE=1; compare with the first real\n"
        "# link map (build.py check()) and record the difference here.\n"
        + "".join(f"{k} {m[k]}\n" for k in keys))


def check(m, cap, cc):
    errors, notes = [], []
    if cap["pool"] - m["pool"] < build.POOL_RESERVE:
        errors.append(f"pool headroom {cap['pool'] - m['pool']} B < {build.POOL_RESERVE} B")
    for key in ("ram", "noinit"):
        if m[key] > cap[key]:
            errors.append(f"{key} {m[key]} B over its region ({cap[key]} B)")
    if m["rodata"] + m["strings"] > cap["xip"]:
        errors.append("constants alone exceed the app slot")
    if os.environ.get("BUDGET_UPDATE") == "1":
        write_baseline(m, cc)
        notes.append(f"baseline {BASELINE.relative_to(SRC)} rewritten")
        return errors, notes
    if not BASELINE.exists():
        errors.append(f"no baseline {BASELINE.relative_to(SRC)} (BUDGET_UPDATE=1 writes it)")
        return errors, notes
    base = read_baseline()
    for k in sorted({*base, *(x for x in m if x in TRACKED or x.startswith("set/"))}):
        if k not in base:
            notes.append(f"{k}: {m[k]} B, no baseline (BUDGET_UPDATE=1 adds it)")
        elif k not in m:
            notes.append(f"{k}: in the baseline but not measured any more")
        elif m[k] > base[k] * (1 + TOL) + SLACK:
            errors.append(f"{k}: {m[k]} B over the baseline {base[k]} B (+{(m[k] - base[k]) * 100 / base[k]:.1f} %, limit +{TOL * 100:.0f} %)")
        elif m[k] < base[k] * (1 - TOL) - SLACK:
            notes.append(f"{k}: {m[k]} B, baseline {base[k]} B: smaller (BUDGET_UPDATE=1 to take it)")
    return errors, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compare with tests/ledger_baseline.txt")
    ap.add_argument("--json", metavar="FILE", help="write the numbers as JSON")
    ap.add_argument("--top", type=int, default=5, metavar="N", help="largest objects per region (default 5)")
    ap.add_argument("--samples", action="store_true", help="per-sample table of the factory sets")
    a = ap.parse_args()
    m, cap, stacks, objs, sets, rows, cc = collect()
    if a.json:
        Path(a.json).write_text(json.dumps({"metrics": m, "capacity": cap, "stacks": stacks, "compiler": cc,
                                            "pool_reserve": build.POOL_RESERVE}, indent=1, sort_keys=True) + "\n")
    if a.check:
        errors, notes = check(m, cap, cc)
        print(f"ledger: RAM {fmt(m['ram'])}/{fmt(cap['ram'])}  POOL {fmt(m['pool'])}/{fmt(cap['pool'])}  "
              f"NOINIT {fmt(m['noinit'])}/{fmt(cap['noinit'])}  CONST {fmt(m['rodata'] + m['strings'])}  "
              f"SMP_DATA {fmt(m['smp_data'])}")
        for n in notes:
            print("  note ", n)
        for e in errors:
            print("  FAIL ", e)
        return 1 if errors else 0
    report(m, cap, stacks, objs, sets, rows, cc, a.top, a.samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
