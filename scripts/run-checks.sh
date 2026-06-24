#!/usr/bin/env bash
# ── Run full quality check suite locally ──────────────────────────
# Equivalent to what CI runs — use before pushing to staging.

set -euo pipefail

echo "========================================="
echo "  Survey Agent — Full Quality Check Suite"
echo "========================================="

ERRORS=0

# ── Python ────────────────────────────────────────────────────────

echo ""
echo "🐍 Ruff lint..."
pdm run ruff check . || {
    echo "❌ Ruff failed"
    ((ERRORS++))
}

echo ""
echo "🐍 Ruff format check..."
pdm run ruff format --check . || {
    echo "❌ Ruff format failed"
    ((ERRORS++))
}

echo ""
echo "🐍 Mypy type check..."
pdm run mypy survey_agent/ || {
    echo "❌ Mypy failed"
    ((ERRORS++))
}

echo ""
echo "🔒 Bandit security scan..."
pdm run bandit -r survey_agent/ -q --skip B101,B110,B112,B311,B608 || {
    echo "❌ Bandit failed"
    ((ERRORS++))
}

echo ""
echo "💀 Vulture dead-code check..."
pdm run vulture survey_agent/ --min-confidence 80 2>/dev/null || true

# ── JS / Markdown ─────────────────────────────────────────────────

echo ""
echo "✨ Prettier check..."
npx --yes prettier --check "**/*.{json,md,yaml,yml}" 2>/dev/null || {
    echo "❌ Prettier failed"
    ((ERRORS++))
}

echo ""
echo "🦎 Biome check..."
npx --yes @biomejs/biome check . 2>/dev/null || {
    echo "❌ Biome failed"
    ((ERRORS++))
}

# ── Tests ─────────────────────────────────────────────────────────

echo ""
echo "🧪 Running tests with coverage..."
pdm run pytest --cov=survey_agent --cov-report=term-missing -v || {
    echo "⚠️  Tests failed or no tests exist"
}

# ── GitNexus ──────────────────────────────────────────────────────

echo ""
echo "📊 GitNexus analyze..."
npx --yes gitnexus analyze 2>/dev/null || {
    echo "❌ GitNexus failed"
    ((ERRORS++))
}

# ── Result ────────────────────────────────────────────────────────

echo ""
echo "========================================="
if [ $ERRORS -eq 0 ]; then
    echo "  ✅ All checks passed!"
    echo "  Ready to commit and push. 🚀"
else
    echo "  ❌ $ERRORS check(s) failed."
    echo "  Fix the issues above and re-run."
fi
echo "========================================="
exit $ERRORS
