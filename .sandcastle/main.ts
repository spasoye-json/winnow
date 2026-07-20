import { execSync, execFileSync } from "node:child_process";
import { z } from "zod";
import { createSandbox, claudeCode } from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";
import {
  BASE,
  BRANCH_PREFIX,
  DEFAULT_MODEL,
  FIX_MODEL,
  GATE_SENTENCE,
  HUMAN_LABEL,
  REVIEW_MODEL,
  VERIFY_MODEL,
  QUEUE_LABEL,
  REPO,
  TEST_DIR,
  WORKING_LABEL,
  diffHasSecret,
  editIssueLabels,
  gh,
  hasOpenPr,
  parseVerifierVerdict,
  recordMetric,
  runGate,
} from "./lib";

const MAX_REVIEW_CYCLES = 3;
const MAX_BUILD_ATTEMPTS = 3;
const MAX_VERIFY_ROUNDS = 2;

// The container inherits env we pass to the agent provider, plus every key named
// in .sandcastle/.env (sandcastle's resolveEnv fills each from .env or the host
// process.env) — nothing else from the host env. The agent needs its Claude
// credential; gh is intentionally absent (issue text is injected), so no GitHub
// token is passed in.
const CLAUDE_ENV = {
  CLAUDE_CODE_OAUTH_TOKEN: process.env.CLAUDE_CODE_OAUTH_TOKEN ?? "",
};
if (!CLAUDE_ENV.CLAUDE_CODE_OAUTH_TOKEN) {
  console.error("CLAUDE_CODE_OAUTH_TOKEN is not set — check .sandcastle/.env.");
  process.exit(1);
}
const claude = (model: string) => claudeCode(model, { env: CLAUDE_ENV });

const Verdict = z.object({
  verdict: z.enum(["approve", "request_changes"]),
  findings: z.array(z.string()),
});

type Issue = { number: number; title: string; body: string };

// The reviewer emits its verdict as the last `<verdict>{json}</verdict>` block in
// its output. `extractStructuredOutput` isn't a public export, so parse it here.
function parseVerdict(stdout: string): z.infer<typeof Verdict> {
  const matches = [...stdout.matchAll(/<verdict>([\s\S]*?)<\/verdict>/g)];
  if (matches.length === 0) throw new Error("reviewer emitted no <verdict> block");
  const raw = matches[matches.length - 1][1]
    .trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .trim();
  return Verdict.parse(JSON.parse(raw));
}

// Host-side git/gh. The agent's commits land on the branch ref in this repo and
// the host gh is authenticated, so we push and open the PR from here — mirroring
// the GitHub Actions flow. The container still never needs gh.
const pushBranch = (branch: string): void => {
  execFileSync("git", ["push", "--force-with-lease", "origin", branch], {
    stdio: "inherit",
  });
};
const commitsOnBranch = (branch: string): number =>
  Number(
    execSync(`git rev-list --count ${BASE}..${branch}`, {
      encoding: "utf8",
    }).trim(),
  );

// Open a PR for the branch, or return the number of the existing open one.
function openOrGetPr(issue: Issue, branch: string): string {
  const existing = gh([
    "pr", "list", "--repo", REPO, "--head", branch, "--state", "open",
    "--json", "number", "--jq", ".[0].number // empty",
  ]).trim();
  if (existing) return existing;
  const title = `Fix #${issue.number}: ${issue.title}`.slice(0, 256);
  const url = gh([
    "pr", "create", "--repo", REPO, "--base", BASE, "--head", branch,
    "--title", title,
    "--body", `Closes #${issue.number}\n\nImplemented by the local Sandcastle runner.`,
  ]).trim();
  return url.split("/").pop() ?? "";
}

// Host-side: fetch the triaged queue. The host `gh` is already authenticated;
// the container never sees a GitHub token (issue text is injected).
const issues: Issue[] = JSON.parse(
  execSync(
    `gh issue list --repo ${REPO} --label ${QUEUE_LABEL} --state open --json number,title,body`,
    { encoding: "utf8" },
  ),
);

if (issues.length === 0) {
  console.log(`No "${QUEUE_LABEL}" issues. Nothing to do.`);
  process.exit(0);
}

for (const issue of issues.sort((a, b) => a.number - b.number)) {
  const branch = `${BRANCH_PREFIX}${issue.number}`;
  const startedAt = Date.now();
  const elapsedSeconds = () => Math.round((Date.now() - startedAt) / 1000);
  console.log(`\n=== #${issue.number} ${issue.title} → ${branch} ===`);

  if (hasOpenPr(branch)) {
    console.log(`#${issue.number}: ${branch} already has an open PR — skipping.`);
    continue;
  }
  editIssueLabels(issue.number, [WORKING_LABEL], [QUEUE_LABEL]);

  try {
    await using sandbox = await createSandbox({
      branch,
      baseBranch: BASE,
      sandbox: docker(),
      // The agent only sees files in the worktree (committed files from BASE).
      // Copy the skills in so `tdd`/`thermo` auto-load even before they're
      // committed; harmless once they are.
      copyToWorktree: [".claude/skills"],
    });

    const issueText = `#${issue.number} ${issue.title}\n\n${issue.body}`;
    let runSeq = 0;

    // 1. Implement test-first. The model-invoked `tdd` skill loads itself.
    //    The gate is the orchestrator's own lint+test run in the worktree.
    const implementUntilGreen = async (
      firstFeedback: string,
    ): Promise<"green" | "no-commits" | "red"> => {
      let feedback = firstFeedback;
      for (let attempt = 1; attempt <= MAX_BUILD_ATTEMPTS; attempt++) {
        runSeq += 1;
        await sandbox.run({
          agent: claude(DEFAULT_MODEL),
          promptFile: "./.sandcastle/implement.md",
          promptArgs: { ISSUE: issueText, FEEDBACK: feedback },
          maxIterations: 6,
          completionSignal: "<promise>IMPLEMENTED</promise>",
          name: `implement-${runSeq}`,
        });

        if (commitsOnBranch(branch) === 0) {
          feedback =
            "Your previous run produced NO commits. Implement the issue and commit the code now.";
          continue;
        }
        const gate = runGate(sandbox.worktreePath);
        if (gate.ok) return "green";
        console.log(`#${issue.number}: objective gate red (attempt ${attempt}).`);
        feedback = `A previous attempt left lint or tests FAILING in ${TEST_DIR}. Fix them until ${GATE_SENTENCE} pass. The objective gate output was:\n\n${gate.output}`;
      }
      return commitsOnBranch(branch) === 0 ? "no-commits" : "red";
    };

    let build = await implementUntilGreen("");

    // 2. Correctness gate: a fresh, read-only verifier judges the diff against
    //    the issue's acceptance criteria and edge cases. A fail re-runs the
    //    implementer (bounded); only an explicit pass proceeds.
    let verified = false;
    if (build === "green") {
      for (let round = 1; round <= MAX_VERIFY_ROUNDS; round++) {
        const verify = await sandbox.run({
          agent: claude(VERIFY_MODEL),
          promptFile: "./.sandcastle/verify.md",
          promptArgs: { ISSUE: issueText, BASE },
          name: `verify-${round}`,
        });
        const verdict = parseVerifierVerdict(verify.stdout);
        if (verdict.verdict === "pass") {
          verified = true;
          break;
        }
        if (verdict.verdict === "missing") {
          console.log(
            `#${issue.number}: verifier emitted no VERDICT line (round ${round}) — treating as fail.`,
          );
        } else {
          console.log(`#${issue.number}: correctness verdict = fail (round ${round}).`);
        }
        if (round === MAX_VERIFY_ROUNDS) break;
        const gap = verdict.reason
          ? `The verifier named this specific gap: ${verdict.reason}`
          : "The verifier did not name a specific gap — a stated acceptance criterion or an edge case (boundaries, empty or missing input, error paths) is unmet.";
        build = await implementUntilGreen(
          `An independent verifier judged the change does NOT fully satisfy issue #${issue.number}. ${gap} Re-read the issue and strengthen the implementation and its tests so every acceptance criterion and edge case is covered.`,
        );
        if (build !== "green") break;
      }
    }

    if (build === "no-commits") {
      console.error(`#${issue.number}: agent produced no commits — re-queueing.`);
      editIssueLabels(issue.number, [QUEUE_LABEL], [WORKING_LABEL]);
      recordMetric(issue.number, "no-commits", "", elapsedSeconds());
      continue;
    }
    if (build !== "green") {
      console.error(
        `#${issue.number}: gate still red after ${MAX_BUILD_ATTEMPTS} attempts — handing to a human (branch left for inspection).`,
      );
      editIssueLabels(issue.number, [HUMAN_LABEL], [WORKING_LABEL]);
      recordMetric(issue.number, "gate-red", "", elapsedSeconds());
      continue;
    }
    if (!verified) {
      console.error(
        `#${issue.number}: correctness verifier still fails after ${MAX_VERIFY_ROUNDS} rounds — handing to a human (branch left for inspection).`,
      );
      editIssueLabels(issue.number, [HUMAN_LABEL], [WORKING_LABEL]);
      recordMetric(issue.number, "verify-fail", "", elapsedSeconds());
      continue;
    }
    if (diffHasSecret(sandbox.worktreePath)) {
      console.error(
        `#${issue.number}: high-confidence secret in the diff — safety stop, not pushing.`,
      );
      editIssueLabels(issue.number, [HUMAN_LABEL], [WORKING_LABEL]);
      recordMetric(issue.number, "secret-detected", "", elapsedSeconds());
      continue;
    }

    // 3. Mirror the Actions flow: push the implementation and open a PR.
    pushBranch(branch);
    const prNumber = openOrGetPr(issue, branch);
    console.log(`#${issue.number} → PR #${prNumber}`);

    // 4. Independent review ↔ fix loop. Each run() is a FRESH claude session,
    //    so the reviewer never inherits the implementer's context.
    let lastVerdict: z.infer<typeof Verdict> | undefined;
    let cyclesUsed = 0;
    for (let cycle = 1; cycle <= MAX_REVIEW_CYCLES; cycle++) {
      cyclesUsed = cycle;
      const review = await sandbox.run({
        agent: claude(REVIEW_MODEL),
        promptFile: "./.sandcastle/review.md",
        promptArgs: { BASE },
        name: `review-${cycle}`,
      });

      lastVerdict = parseVerdict(review.stdout);

      if (lastVerdict.verdict === "approve") {
        console.log(`#${issue.number} approved on review cycle ${cycle}.`);
        break;
      }
      if (cycle === MAX_REVIEW_CYCLES) {
        console.log(
          `#${issue.number} still has ${lastVerdict.findings.length} finding(s) after ${cycle} cycles.`,
        );
        break;
      }

      await sandbox.run({
        agent: claude(FIX_MODEL), // mechanical fixes → cheaper tier
        promptFile: "./.sandcastle/fix.md",
        promptArgs: { FINDINGS: lastVerdict.findings.map((f) => `- ${f}`).join("\n") },
        maxIterations: 4,
        completionSignal: "<promise>FIXED</promise>",
        name: `fix-${cycle}`,
      });
    }

    // 5. Push review/fix commits and post the reviewer's outcome on the PR.
    //    The secret scan and the objective gate run once more first — a review
    //    fix must not leak a key or land red, and either one withholds the fix
    //    commits entirely. Scan for secrets before the gate so a leaked key
    //    short-circuits without wasting a full lint and test run.
    if (diffHasSecret(sandbox.worktreePath)) {
      console.error(
        `#${issue.number}: secret appeared in the diff during review — safety stop, not pushing the fix commits.`,
      );
      editIssueLabels(issue.number, [HUMAN_LABEL], [WORKING_LABEL]);
      recordMetric(issue.number, "secret-detected", cyclesUsed, elapsedSeconds());
      continue;
    }
    const finalGate = runGate(sandbox.worktreePath);
    if (!finalGate.ok) {
      console.error(
        `#${issue.number}: objective gate red after the review loop — withholding the fix commits.`,
      );
      gh([
        "pr", "comment", prNumber, "--repo", REPO, "--body",
        `**Automated review produced fix commits, but they left the objective gate RED** (lint or tests failing in \`${TEST_DIR}\`), so they were NOT pushed. The branch is left local for inspection.`,
      ]);
      editIssueLabels(issue.number, [HUMAN_LABEL], [WORKING_LABEL]);
      recordMetric(issue.number, "gate-red", cyclesUsed, elapsedSeconds());
      continue;
    }
    pushBranch(branch);
    const summary =
      lastVerdict?.verdict === "approve"
        ? "**Automated review: approved.** No outstanding findings."
        : `**Automated review: changes still open** after ${MAX_REVIEW_CYCLES} cycle(s):\n\n${(lastVerdict?.findings ?? [])
            .map((f) => `- ${f}`)
            .join("\n")}`;
    gh(["pr", "comment", prNumber, "--repo", REPO, "--body", summary]);

    editIssueLabels(issue.number, [], [WORKING_LABEL]);
    recordMetric(
      issue.number,
      lastVerdict?.verdict === "approve" ? "approved" : "changes-open",
      cyclesUsed,
      elapsedSeconds(),
    );
    console.log(`#${issue.number} → PR #${prNumber} ready (not merged).`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`#${issue.number} failed: ${msg} — re-queueing, branch left for inspection.`);
    editIssueLabels(issue.number, [QUEUE_LABEL], [WORKING_LABEL]);
    recordMetric(issue.number, "error", "", elapsedSeconds());
  }
}
