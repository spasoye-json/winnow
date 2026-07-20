export const config = {
  repo: process.env.GH_REPO ?? "spasoye-json/winnow",
  base: process.env.BASE_BRANCH ?? "master",
  testDir: process.env.TEST_DIR ?? ".",
  gateCommands: ["uv run ruff check .", "uv run pytest"],
  queueLabel: "ready-for-agent",
  workingLabel: "claude-working",
  humanLabel: "ready-for-human",
  // Model tiers: Fable 5 on the judgment gates (verify, review), Opus 4.8 on
  // everything else, Sonnet 5 for mechanical fix application.
  defaultModel: "claude-opus-4-8",
  verifyModel: "claude-fable-5",
  reviewModel: "claude-fable-5",
  fixModel: "claude-sonnet-5",
  localBranchPrefix: "sandcastle/issue-",
};
