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
command -v pdm >/dev/null 2>&1 || {
    echo "❌ pdm required — install via: curl -sSL https://pdm-project.org/install-pdm.py | python3"
    exit 1
}
command -v uv >/dev/null 2>&1 || {
    echo "❌ uv required — install via: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
}
command -v node >/dev/null 2>&1 || {
    echo "❌ Node.js required (install via nvm or brew)"
    exit 1
}
command -v pnpm >/dev/null 2>&1 || {
    echo "❌ pnpm required — install via: npm i -g pnpm"
    exit 1
}

echo "   ✅ Python $(python3 --version)"
echo "   ✅ Node $(node --version)"
echo "   ✅ pnpm $(pnpm --version)"

# ── Install Python deps (PDM with uv) ────────────────────────────
echo ""
echo "📦 Installing Python dependencies..."
pdm install -d --frozen --no-self 2>/dev/null || pdm install -d
echo "   ✅ Python deps installed"

# ── Install Playwright browsers ──────────────────────────────────
echo ""
echo "🌐 Installing Playwright browsers..."
pdm run playwright install chromium 2>/dev/null || true
echo "   ✅ Playwright installed"

# ── Install JS deps (czg, lefthook, prettier, etc.) ────────────
echo ""
echo "📦 Installing Node.js tooling..."
pnpm install
echo "   ✅ Node tooling installed (lefthook, czg, commitizen, commitlint, prettier, biome)"

# ── Verify lefthook hooks are installed ──────────────────────────
echo ""
echo "🔗 Verifying git hooks..."
pnpm exec lefthook install 2>/dev/null || true
echo "   ✅ Git hooks active"

# ── Final summary ────────────────────────────────────────────────
echo ""
echo "========================================="
echo "  ✅ Setup complete!"
echo "========================================="
echo ""
echo "  📝 Commit helper:     pnpm commit        (czg)"
echo "  🧪 Run tests:         pdm run pytest"
echo "  🐍 Lint Python:       pdm run ruff check"
echo "  ✨ Format all:        pnpm format         (prettier)"
echo ""
echo "  Next: configure your Git remote and profile:"
echo "    survey-agent profile setup"
echo "    survey-agent credentials set branded-surveys --username you@example.com"
echo "    survey-agent run"
echo ""
