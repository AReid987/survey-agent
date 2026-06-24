#!/usr/bin/env bash
# ── Shift-Left QA: Coverage Gate + Auto-Test Generation ────────────
# Called by lefthook pre-commit. Runs pytest with coverage; if below
# threshold, auto-generates tests via Claude API and retries.
#
# Static analysis (ruff, mypy, bandit, etc.) is handled by sibling
# lefthook hooks — this script focuses exclusively on test coverage.
#
# Usage:
#   scripts/shift-left.sh              # default: 80% threshold, 3 retries
#   scripts/shift-left.sh 85 5         # 85% threshold, 5 retries
#
# Environment:
#   ANTHROPIC_API_KEY or CLAUDE_API_KEY — enables AI test generation
#   SHIFT_LEFT_MAX_RETRIES             — override max retries (default 3)
#   SHIFT_LEFT_THRESHOLD               — override coverage threshold (default 80)

set -euo pipefail

THRESHOLD="${1:-${SHIFT_LEFT_THRESHOLD:-80}}"
MAX_RETRIES="${2:-${SHIFT_LEFT_MAX_RETRIES:-3}}"

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$PROJECT_ROOT/scripts"

cd "$PROJECT_ROOT"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}🛡️  Shift-Left QA Pipeline${NC}"
echo -e "   Threshold: ${THRESHOLD}% | Max retries: ${MAX_RETRIES}"

# ── Coverage Gate with Auto-Fix Loop ──────────────────────────────
attempt=0
coverage_met=false
total_pct="0"

while [ $attempt -lt $MAX_RETRIES ]; do
    attempt=$((attempt + 1))
    echo -e "\n${CYAN}  ── Attempt ${attempt}/${MAX_RETRIES} ──${NC}"

    # Run coverage check
    coverage_json=$("$SCRIPTS/coverage-check.sh" "$THRESHOLD" 2>/dev/null || true)
    if [ -z "$coverage_json" ]; then
        echo -e "  ${RED}Failed to run coverage check${NC}"
        exit 1
    fi

    echo -e "  ${YELLOW}📝 Coverage gap — triggering auto-test writer...${NC}"
    echo "$coverage_json" | pdm run python3 "$SCRIPTS/auto-test-writer.py"

    # Parse result
    total_pct=$(echo "$coverage_json" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['total_pct'])" 2>/dev/null || echo "0")
    is_ok=$(echo "$coverage_json" | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['ok'])" 2>/dev/null || echo "False")

    echo -e "  Coverage: ${BOLD}${total_pct}%${NC} (threshold: ${THRESHOLD}%)"

    if [ "$is_ok" = "True" ]; then
        coverage_met=true
        break
    fi

    if [ $attempt -ge $MAX_RETRIES ]; then
        echo -e "\n  ${RED}❌ Coverage ${total_pct}% < ${THRESHOLD}% after ${MAX_RETRIES} attempts${NC}"
        break
    fi

    # Auto-generate tests for uncovered code
    echo -e "  ${YELLOW}📝 Coverage gap — triggering auto-test writer...${NC}"
    echo "$coverage_json" | python3 "$SCRIPTS/auto-test-writer.py"

    echo -e "  ${CYAN}🔄 Re-running coverage check...${NC}"
done

# ── Final Test Run ────────────────────────────────────────────────
echo ""
echo -n "  Running full test suite... "
test_output=$(pdm run pytest --no-header -q --tb=short \
    --cov=survey_agent --cov-report=term-missing \
    2>&1) || true

test_exit=$?
if [ $test_exit -eq 0 ]; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    echo "$test_output" | tail -20
    echo -e "\n${RED}❌ Tests failing — fix before committing.${NC}"
    exit 1
fi

# ── Result ────────────────────────────────────────────────────────
if [ "$coverage_met" = true ]; then
    echo -e "\n${GREEN}✅ Shift-Left QA passed${NC} — coverage ${total_pct}% ≥ ${THRESHOLD}%"
    exit 0
else
    echo -e "\n${RED}❌ Shift-Left QA failed${NC} — coverage ${total_pct}% < ${THRESHOLD}%"
    echo -e "${YELLOW}   Auto-generated tests may need manual review.${NC}"
    echo -e "${YELLOW}   Run: pdm run pytest --cov=survey_agent --cov-report=term-missing${NC}"
    exit 1
fi
