import { fetchIssueText, gh, safeSh, sh } from "./common";
import { parseDiffLines } from "./diff-lines";
import { config } from "../../sandcastle.config";

export interface ReviewThreadComment {
  readonly commentId: string;
  readonly threadId: string;
  readonly path: string | null;
  readonly line: number | null;
  readonly author: string;
  readonly body: string;
}

export interface PullRequestContext {
  readonly prTitle: string;
  readonly prBody: string;
  readonly issueNumber: string;
  readonly issueTitle: string;
  readonly linkedIssue: string;
  readonly diff: string;
  readonly prCommentsJson: string;
  readonly validReplyIds: Set<string>;
}

export const currentDiffLines = (): Map<string, Set<number>> =>
  parseDiffLines(diffToBase());

const diffToBase = (): string =>
  safeSh(`git diff ${config.base}...HEAD`) ||
  sh(`git diff ${config.base}..HEAD`);

const THREADS_PAGE_SIZE = 100;
const COMMENTS_PAGE_SIZE = 50;

export const fetchPullRequestContext = (
  prNumber: string,
): PullRequestContext => {
  const prView = JSON.parse(
    gh(["pr", "view", prNumber, "--json", "title,body,comments"]),
  ) as {
    title: string;
    body?: string | null;
    comments: {
      author?: { login: string } | null;
      body: string;
      createdAt?: string;
    }[];
  };

  const issueMatch = (prView.body ?? "").match(
    /(?:closes|fixes|resolves)\s+#(\d+)/i,
  );
  const issueNumber = issueMatch?.[1] ?? "";
  const issueTitle = issueNumber
    ? safeSh(`gh issue view ${issueNumber} --json title --jq .title`).trim()
    : "";
  const linkedIssue =
    (issueNumber && fetchIssueText(issueNumber)) || "(no linked issue found)";

  const reviews = JSON.parse(
    gh(["api", `repos/{owner}/{repo}/pulls/${prNumber}/reviews`]),
  ) as {
    user?: { login: string } | null;
    body?: string | null;
    state: string;
    submitted_at?: string | null;
  }[];

  const ghRepo = process.env.GH_REPO ?? "";
  if (!ghRepo.includes("/")) {
    throw new Error(
      `GH_REPO must be set to "owner/repo" to fetch review threads; got "${ghRepo}".`,
    );
  }
  const [owner, repo] = ghRepo.split("/");
  const query = `
query($owner:String!,$repo:String!,$number:Int!) {
  repository(owner:$owner,name:$repo) {
    pullRequest(number:$number) {
      reviewThreads(first:${THREADS_PAGE_SIZE}) {
        nodes {
          id
          isResolved
          comments(first:${COMMENTS_PAGE_SIZE}) {
            nodes {
              id
              path
              line
              originalLine
              body
              author { login }
            }
          }
        }
      }
    }
  }
}`;

  const threadsParsed = JSON.parse(
    gh([
      "api",
      "graphql",
      "-F",
      `owner=${owner}`,
      "-F",
      `repo=${repo}`,
      "-F",
      `number=${prNumber}`,
      "-f",
      `query=${query}`,
    ]),
  ) as {
    data?: {
      repository?: {
        pullRequest?: {
          reviewThreads?: {
            nodes?: {
              id: string;
              isResolved: boolean;
              comments: {
                nodes: {
                  id: string;
                  path: string | null;
                  line: number | null;
                  originalLine: number | null;
                  body: string;
                  author?: { login: string } | null;
                }[];
              };
            }[];
          };
        };
      };
    };
  };

  const allThreads =
    threadsParsed.data?.repository?.pullRequest?.reviewThreads?.nodes ?? [];
  if (allThreads.length === THREADS_PAGE_SIZE) {
    console.warn(
      `PR #${prNumber} has ${THREADS_PAGE_SIZE}+ review threads; the query is not paginated, so some threads may be missing.`,
    );
  }
  for (const thread of allThreads) {
    if (thread.comments.nodes.length === COMMENTS_PAGE_SIZE) {
      console.warn(
        `Review thread ${thread.id} has ${COMMENTS_PAGE_SIZE}+ comments; the query is not paginated, so some comments may be missing.`,
      );
    }
  }

  const unresolvedThreads = allThreads.filter((thread) => !thread.isResolved);

  const reviewThreads: ReviewThreadComment[] = unresolvedThreads.flatMap(
    (thread) =>
      thread.comments.nodes.map((comment) => ({
        commentId: comment.id,
        threadId: thread.id,
        path: comment.path,
        line: comment.line ?? comment.originalLine,
        author: comment.author?.login ?? "unknown",
        body: comment.body,
      })),
  );

  const prComments = {
    issue_comments: prView.comments.map((comment) => ({
      author: comment.author?.login ?? "unknown",
      body: comment.body,
      createdAt: comment.createdAt,
    })),
    review_summaries: reviews
      .filter((review) => review.body && review.body.trim().length > 0)
      .map((review) => ({
        author: review.user?.login ?? "unknown",
        state: review.state,
        body: review.body,
        submittedAt: review.submitted_at,
      })),
    review_threads: reviewThreads,
  };

  const diff = diffToBase();

  return {
    prTitle: prView.title,
    prBody: prView.body ?? "",
    issueNumber,
    issueTitle,
    linkedIssue,
    diff,
    prCommentsJson: JSON.stringify(prComments, null, 2),
    validReplyIds: new Set(reviewThreads.map((comment) => comment.commentId)),
  };
};
