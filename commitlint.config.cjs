// ── Commitlint ────────────────────────────────────────────────────
// Enforces Conventional Commits format on every commit message.
// https://www.conventionalcommits.org/
//
// Allowed types:
//   feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert
//
// Examples:
//   feat(orchestrator): add multi-session concurrency
//   fix(browser): handle timeout on captcha detection
//   ci: add GitHub Actions staging deployment workflow

module.exports = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "header-max-length": [2, "always", 100],
    "scope-case": [2, "always", "lower-case"],
    "subject-case": [2, "never", ["sentence-case", "start-case", "pascal-case", "upper-case"]],
    "subject-empty": [2, "never"],
    "type-empty": [2, "never"],
    "type-enum": [
      2,
      "always",
      [
        "feat",
        "fix",
        "docs",
        "style",
        "refactor",
        "perf",
        "test",
        "build",
        "ci",
        "chore",
        "revert",
      ],
    ],
  },
};
