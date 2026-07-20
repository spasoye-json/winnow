import * as fs from "node:fs";
import * as path from "node:path";
import * as sandcastle from "@ai-hero/sandcastle";
import { noSandbox } from "@ai-hero/sandcastle/sandboxes/no-sandbox";
import { config } from "../../sandcastle.config";
import {
  claudeAgent,
  fail,
  required,
  sh,
  writeJson,
  writeText,
} from "../shared/common";
import {
  currentDiffLines,
  fetchPullRequestContext,
} from "../shared/review-context";
import {
  filterInlineComments,
  filterReplies,
  reviewOutputSchema,
} from "../shared/review-output";
import { runWithExtraction } from "../shared/run-with-extraction";

const PR_NUMBER = required("PR_NUMBER");
const BRANCH = required("BRANCH");

try {
  const context = fetchPullRequestContext(PR_NUMBER);

  // noSandbox forwards process.env to the agent; drop the write-scope GitHub token
  // so injected PR or issue text cannot direct the agent to use it. All gh reads
  // are done, and the checkout sets persist-credentials: false so it is not in
  // .git/config either.
  delete process.env.GH_TOKEN;
  delete process.env.GITHUB_TOKEN;

  const result = await runWithExtraction({
    name: `review-pr-${PR_NUMBER}`,
    agent: claudeAgent(config.reviewModel),
    sandbox: noSandbox(),
    logging: { type: "stdout" },
    promptFile: path.join(import.meta.dirname, "prompt.md"),
    promptArgs: {
      PR_NUMBER,
      BRANCH,
      PR_TITLE: context.prTitle,
      ISSUE_NUMBER: context.issueNumber || "(none)",
      ISSUE_TITLE: context.issueTitle || "(no linked issue)",
      LINKED_ISSUE: context.linkedIssue,
      DIFF_TO_MAIN: context.diff,
      PR_COMMENTS_JSON: context.prCommentsJson,
    },
    output: sandcastle.Output.object({
      tag: "output",
      schema: reviewOutputSchema,
    }),
    extractionPrompt: fs.readFileSync(
      path.join(import.meta.dirname, "extraction.md"),
      "utf8",
    ),
  });

  // The agent may have committed, shifting line numbers; validate against the
  // post-run diff, which matches the commit_id the review is posted against.
  const validInlineComments = filterInlineComments(
    result.output.inlineComments,
    currentDiffLines(),
  );
  const validReplies = filterReplies(
    result.output.replies,
    context.validReplyIds,
  );
  const headSha = sh("git rev-parse HEAD").trim();

  // Surface a failed spec check at the top of the posted review so the human
  // merge gate cannot miss it; the summary alone proved glossable in practice.
  const reviewBody = result.output.specConformant
    ? result.output.summary
    : [
        "**Spec conformance: FAILED.** Unmet acceptance criteria from the linked issue:",
        ...result.output.unmetCriteria.map((criterion) => `- ${criterion}`),
        "",
        result.output.summary,
      ].join("\n");

  writeJson("review_payload.json", {
    commit_id: headSha,
    event: "COMMENT",
    body: reviewBody,
    comments: validInlineComments.map((comment) => ({
      path: comment.path,
      line: comment.line,
      side: "RIGHT",
      body: comment.body,
    })),
  });
  writeJson("replies.json", validReplies);
  writeText("summary.md", reviewBody);

  console.log("Review complete.");
  console.log(`Spec conformant: ${result.output.specConformant}.`);
  console.log(`Commits: ${result.commits.length}.`);
  console.log(`Inline comments: ${validInlineComments.length}.`);
  console.log(`Replies: ${validReplies.length}.`);
} catch (error) {
  fail(error instanceof Error ? error.message : String(error));
}
