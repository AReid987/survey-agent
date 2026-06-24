#!/usr/bin/env bash
# ── Coverage Gate ───────────────────────────────────────────────────
# Runs pytest with coverage, compares against threshold.
# Output: JSON on stdout with {ok, total_pct, threshold, uncovered: [...]}
# Exit 0 if coverage >= threshold, exit 1 otherwise.

set -euo pipefail

THRESHOLD="${1:-80}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$PROJECT_ROOT"

# ── Run pytest with coverage ────────────────────────────────────────
# Suppress pytest stdout noise; we only care about the XML + .coverage
pdm run pytest \
    --cov=survey_agent \
    --cov-report=xml \
    --no-header \
    -q \
    >/dev/null 2>&1 || true

# ── Parse coverage XML ──────────────────────────────────────────────
python3 - "$THRESHOLD" <<'PYEOF'
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

threshold = int(sys.argv[1])
xml_path = Path("coverage.xml")

if not xml_path.exists():
    print('{"ok": false, "error": "coverage.xml not found", "total_pct": 0}')
    sys.exit(1)

tree = ET.parse(xml_path)
root = tree.getroot()

# Extract overall rate
rate = float(root.attrib.get("line-rate", 0))
total_pct = round(rate * 100, 1)

# Find uncovered files
uncovered = []
for pkg in root.iter("package"):
    pkg_name = pkg.attrib.get("name", "")
    for cls in pkg.iter("class"):
        cls_rate = float(cls.attrib.get("line-rate", 0))
        cls_name = cls.attrib.get("name", "")
        filename = cls.attrib.get("filename", "")
        if cls_rate < (threshold / 100.0):
            # Count uncovered lines
            missing = 0
            total = 0
            for line in cls.iter("line"):
                total += 1
                hits = int(line.attrib.get("hits", 0))
                if hits == 0:
                    missing += 1
            uncovered.append({
                "file": filename,
                "pct": round(cls_rate * 100, 1),
                "missing": missing,
                "total": total,
            })

import json
result = {
    "ok": total_pct >= threshold,
    "total_pct": total_pct,
    "threshold": threshold,
    "uncovered": sorted(uncovered, key=lambda x: x["pct"]),
}
print(json.dumps(result))
sys.exit(0 if result["ok"] else 1)
PYEOF
