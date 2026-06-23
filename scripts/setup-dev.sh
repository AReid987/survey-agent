#!/usr/bin/env bash
# ── Development Environment Setup ────────────────────────────────
# Run this once after cloning to install all tooling.

set -euo pipefail

echo "========================================="
echo "  Survey Agent — Dev Environment Setup"
echo "========================================="

# ── Prerequisites check ──────────────────────────────────────────
echo ""
echo "🔍 Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || {
    echo "❌ python3 required"
    exit 1
}
command -v pip3 >/dev/null 2>&1 || {
    echo "❌ pip3 required"
    exit 1
}
command -v node >/dev/null 2>&1 || {
    echo "❌ Node.js required (install via nvm or brew)"
    exit 1
}
command -v npm >/dev/null 2>&1 || {
    echo "❌ npm required"
    exit 1
}

echo "   ✅ Python $(python3 --version)"
echo "   ✅ Node $(node --version)"

# ── Install Python deps (PDM) ────────────────────────────────────
echo ""
echo "📦 Installing Python dependencies..."
pip3 install pdm --quiet
pdm install -d
echo "   ✅ Python deps installed"

# ── Install Playwright browsers ──────────────────────────────────
echo ""
echo "🌐 Installing Playwright browsers..."
pdm run playwright install chromium 2>/dev/null || true
echo "   ✅ Playwright installed"

# ── Install JS deps (czg, lefthook, prettier, etc.) ────────────
echo ""
echo "📦 Installing Node.js tooling..."
npm install
echo "   ✅ Node tooling installed (lefthook, czg, commitizen, commitlint, prettier, biome)"

# ── Verify lefthook hooks are installed ──────────────────────────
echo ""
echo "🔗 Verifying git hooks..."
npx lefthook install 2>/dev/null || true
echo "   ✅ Git hooks active"

# ── Final summary ────────────────────────────────────────────────
echo ""
echo "========================================="
echo "  ✅ Setup complete!"
echo "========================================="
echo ""
echo "  📝 Commit helper:     npm run commit    (czg)"
echo "  🧪 Run tests:         pdm run pytest"
echo "  🐍 Lint Python:       pdm run ruff check"
echo "  ✨ Format all:        npm run format    (prettier)"
echo ""
echo "  Next: configure your Git remote and profile:"
echo "    survey-agent profile setup"
echo "    survey-agent credentials set branded-surveys --username you@example.com"
echo "    survey-agent run"
echo ""
