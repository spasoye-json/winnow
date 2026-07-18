import * as sandcastle from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";
import {
  BASE,
  BRANCH_PREFIX,
  GATE_SENTENCE,
  QUEUE_LABEL,
  TEST_DIR,
  diffHasSecret,
  gh,
  recordMetric,
  runGate,
} from "./lib";

const MAX_PARALLEL = 4;
const MAX_GATE_RETRIES = 2;

// Matt Pocock's local runner, adapted: single pass, NO auto-merge. Plan the
// unblocked issues, implement + review each in parallel, then leave the
// branches for you to test and merge by hand. (main.ts is the simpler serial
// drain; this one parallelises across independent issues.)

// The container has no gh and no GitHub token, so all GitHub reads happen here
// on the host and are injected into the prompts via promptArgs.
const issuesJson = gh([
  "issue", "list", "--state", "open", "--label", QUEUE_LABEL,
  "--limit", "100", "--json", "number,title,body,labels,comments",
  "--jq",
  "[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]",
]);

// Phase 1: Plan — orchestrator picks parallelizable, unblocked issues.
const plan = await sandcastle.run({
  sandbox: docker(),
  name: "Planner",
  agent: sandcastle.claudeCode("claude-opus-4-8"),
  promptFile: "./.sandcastle/plan-prompt.md",
  promptArgs: { ISSUES_JSON: issuesJson, BRANCH_PREFIX },
});

const planMatch = plan.stdout.match(/<plan>([\s\S]*?)<\/plan>/);
if (!planMatch) {
  throw new Error(
    "Orchestrator did not produce a <plan> tag.\n\n" + plan.stdout,
  );
}

const { issues } = JSON.parse(planMatch[1]) as {
  issues: { number: number; title: string; branch: string }[];
};

// The plan is LLM output derived from untrusted issue text — validate every
// entry before using it as a branch name or gh argument.
for (const entry of issues) {
  const validNumber = Number.isInteger(entry.number) && entry.number > 0;
  const validBranch =
    typeof entry.branch === "string" &&
    entry.branch === `${BRANCH_PREFIX}${entry.number}`;
  const validTitle = typeof entry.title === "string" && entry.title.trim() !== "";
  if (!validNumber || !validBranch || !validTitle) {
    throw new Error(
      `Planner produced an invalid plan entry: ${JSON.stringify(entry)} — ` +
        `number must be a positive integer, branch must be ${BRANCH_PREFIX}<number> ` +
        "with the digits matching number, and title must be a non-empty string.",
    );
  }
}

if (issues.length === 0) {
  console.log("No unblocked issues to work on. Exiting.");
  process.exit(0);
}

console.log(
  `Planning complete. ${issues.length} issue(s) to work in parallel:`,
);
for (const issue of issues) {
  console.log(`  #${issue.number}: ${issue.title} → ${issue.branch}`);
}

// Phase 2: Execute + Review — implement then review each branch, max 4 in parallel.
let running = 0;
const queue: (() => void)[] = [];
const acquire = () =>
  running < MAX_PARALLEL
    ? (running++, Promise.resolve())
    : new Promise<void>((resolve) => queue.push(resolve));
const release = () => {
  running--;
  const next = queue.shift();
  if (next) {
    running++;
    next();
  }
};

type IssueOutcome = {
  commits: number;
  gateOk: boolean;
  secret: boolean;
};

const settled = await Promise.allSettled(
  issues.map(async (issue): Promise<IssueOutcome> => {
    await acquire();
    const startedAt = Date.now();
    try {
      const issueContext = gh([
        "issue", "view", String(issue.number), "--comments",
      ]);
      await using sandbox = await sandcastle.createSandbox({
        sandbox: docker(),
        branch: issue.branch,
        baseBranch: BASE,
        // Mirror main.ts: vendor the skills so tdd/thermo auto-load. The agent
        // installs the package deps it needs itself (see implement-prompt.md);
        // there is no root build in this monorepo.
        copyToWorktree: [".claude/skills"],
      });

      const implementArgs = {
        ISSUE_NUMBER: String(issue.number),
        ISSUE_TITLE: issue.title,
        BRANCH: issue.branch,
        ISSUE_CONTEXT: issueContext,
        BASE,
      };
      const result = await sandbox.run({
        name: "Implementer #" + issue.number,
        agent: sandcastle.claudeCode("claude-opus-4-8"),
        promptFile: "./.sandcastle/implement-prompt.md",
        promptArgs: { ...implementArgs, FEEDBACK: "" },
      });

      let commits = result.commits.length;
      if (commits === 0) {
        recordMetric(
          issue.number,
          "no-commits",
          "",
          Math.round((Date.now() - startedAt) / 1000),
        );
        return { commits: 0, gateOk: false, secret: false };
      }

      let gate = runGate(sandbox.worktreePath);
      for (let retry = 1; !gate.ok && retry <= MAX_GATE_RETRIES; retry++) {
        console.log(
          `#${issue.number}: objective gate red — re-running implementer (retry ${retry}).`,
        );
        const fixRun = await sandbox.run({
          name: `Implementer #${issue.number} (gate retry ${retry})`,
          agent: sandcastle.claudeCode("claude-opus-4-8"),
          promptFile: "./.sandcastle/implement-prompt.md",
          promptArgs: {
            ...implementArgs,
            FEEDBACK: `A previous attempt left lint or tests FAILING in ${TEST_DIR}. Fix them until ${GATE_SENTENCE} pass. The objective gate output was:\n\n${gate.output}`,
          },
        });
        commits += fixRun.commits.length;
        gate = runGate(sandbox.worktreePath);
      }

      if (gate.ok) {
        await sandbox.run({
          name: "Reviewer #" + issue.number,
          agent: sandcastle.claudeCode("claude-opus-4-8"),
          promptFile: "./.sandcastle/review-prompt.md",
          promptArgs: {
            ISSUE_NUMBER: String(issue.number),
            ISSUE_TITLE: issue.title,
            BRANCH: issue.branch,
            ISSUE_CONTEXT: issueContext,
            BASE,
          },
        });
        gate = runGate(sandbox.worktreePath);
      }

      const secret = diffHasSecret(sandbox.worktreePath);
      recordMetric(
        issue.number,
        secret ? "secret-detected" : gate.ok ? "ready" : "gate-red",
        "",
        Math.round((Date.now() - startedAt) / 1000),
      );
      return { commits, gateOk: gate.ok, secret };
    } finally {
      release();
    }
  }),
);

for (const [i, outcome] of settled.entries()) {
  if (outcome.status === "rejected") {
    console.error(
      `  ✗ #${issues[i].number} (${issues[i].branch}) failed: ${outcome.reason}`,
    );
  }
}

const completed = settled
  .map((outcome, i) => ({ outcome, issue: issues[i] }))
  .filter(
    (entry) =>
      entry.outcome.status === "fulfilled" && entry.outcome.value.commits > 0,
  )
  .map((entry) => ({
    issue: entry.issue,
    result: (entry.outcome as PromiseFulfilledResult<IssueOutcome>).value,
  }));

console.log(
  `\nExecution complete. ${completed.length} branch(es) with commits — test and merge by hand:`,
);
for (const { issue, result } of completed) {
  const warnings = [
    result.gateOk ? "" : "GATE RED",
    result.secret ? "SECRET IN DIFF — do not push" : "",
  ]
    .filter(Boolean)
    .join(", ");
  console.log(`  ${issue.branch}${warnings ? `  [${warnings}]` : ""}`);
}
