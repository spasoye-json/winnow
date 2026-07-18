export const config = {
  repo: process.env.GH_REPO ?? "spasoye-json/winnow",
  base: process.env.BASE_BRANCH ?? "master",
  testDir: process.env.TEST_DIR ?? ".",
  gateCommands: ["uv run ruff check .", "uv run pytest"],
  queueLabel: "ready-for-agent",
  workingLabel: "claude-working",
  humanLabel: "ready-for-human",
  localBranchPrefix: "sandcastle/issue-",
};
