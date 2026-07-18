import * as path from "node:path";
import * as sandcastle from "@ai-hero/sandcastle";
import { noSandbox } from "@ai-hero/sandcastle/sandboxes/no-sandbox";
import {
  claudeAgent,
  fail,
  fetchIssueText,
  required,
  sh,
} from "../shared/common";
import { config } from "../../sandcastle.config";

const ISSUE_NUMBER = required("ISSUE_NUMBER");
const ISSUE_TITLE = required("ISSUE_TITLE");
const BRANCH = required("BRANCH");

try {
  const issueContext =
    fetchIssueText(ISSUE_NUMBER) ||
    `Issue #${ISSUE_NUMBER}: ${ISSUE_TITLE}`;

  // noSandbox forwards process.env to the agent; drop the write-scope GitHub token
  // so injected issue text cannot direct the agent to use it. All gh reads are
  // done, and the checkout sets persist-credentials: false so it is not in
  // .git/config either.
  delete process.env.GH_TOKEN;
  delete process.env.GITHUB_TOKEN;

  const result = await sandcastle.run({
    name: `implement-#${ISSUE_NUMBER}`,
    agent: claudeAgent(),
    sandbox: noSandbox(),
    logging: { type: "stdout" },
    promptFile: path.join(import.meta.dirname, "prompt.md"),
    promptArgs: {
      ISSUE_NUMBER,
      ISSUE_TITLE,
      BRANCH,
      ISSUE_CONTEXT: issueContext,
    },
  });

  const commitsAhead = Number(sh(`git rev-list --count ${config.base}..HEAD`).trim());
  if (!Number.isFinite(commitsAhead) || commitsAhead === 0) {
    fail("Agent finished but no commits were made on the branch.");
  }

  console.log(`Implementation produced ${commitsAhead} commit(s).`);
  console.log(`Commits this run: ${result.commits.length}.`);
} catch (error) {
  fail(error instanceof Error ? error.message : String(error));
}
