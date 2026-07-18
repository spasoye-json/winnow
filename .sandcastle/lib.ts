import { execSync, execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { defaultImageName } from "@ai-hero/sandcastle/sandboxes/docker";
import { config } from "./sandcastle.config";

export const REPO = config.repo;
export const QUEUE_LABEL = config.queueLabel;
export const WORKING_LABEL = config.workingLabel;
export const HUMAN_LABEL = config.humanLabel;
export const BASE = config.base;
export const TEST_DIR = config.testDir;
export const GATE_COMMANDS = config.gateCommands;
export const BRANCH_PREFIX = config.localBranchPrefix;
export const GATE_SENTENCE = GATE_COMMANDS.map((c) => `'${c}'`).join(" and ");

export const gh = (args: string[]): string =>
  execFileSync("gh", args, { encoding: "utf8" });

export const editIssueLabels = (
  issueNumber: number,
  add: string[],
  remove: string[],
): void => {
  const args = ["issue", "edit", String(issueNumber), "--repo", REPO];
  for (const label of add) args.push("--add-label", label);
  for (const label of remove) args.push("--remove-label", label);
  try {
    gh(args);
  } catch {
    console.error(
      `#${issueNumber}: label update failed (add: ${add.join(",") || "-"}, remove: ${remove.join(",") || "-"}).`,
    );
  }
};

export const hasOpenPr = (branch: string): boolean =>
  gh([
    "pr", "list", "--repo", REPO, "--head", branch, "--state", "open",
    "--json", "number", "--jq", "length",
  ]).trim() !== "0";

export type GateResult = { ok: boolean; output: string };

// Where sandcastle's docker provider mounts the worktree inside the container.
const SANDBOX_WORKTREE = "/home/agent/workspace";

// A wedged test or leftover watch mode can hang a gate command forever. Cap
// the wall clock so a stuck run fails loudly instead of pinning the runner.
// Tunable via GATE_TIMEOUT_SECONDS; falsy or unset falls back to one hour.
const GATE_TIMEOUT_SECONDS = Number(process.env.GATE_TIMEOUT_SECONDS) || 3600;

// The gate runs INSIDE the sandbox container, never on the host: the worktree
// holds agent-written code, so `npm ci`, lint, and tests must not execute
// host-side. The public Sandbox handle exposes no exec, so we `docker run` the
// same image sandcastle's docker provider uses, with the same UID/GID mapping
// and the same hardening flags (dropped caps, no privilege escalation, init).
export const runGate = (worktreePath: string): GateResult => {
  const image = defaultImageName(process.cwd());
  // The docker provider builds this image on the first agent run. If the gate
  // runs first on a fresh clone the image is absent, and `docker run` would
  // try to pull a nonexistent image and fail as a misleading red gate. Report
  // the missing image plainly instead.
  try {
    execFileSync("docker", ["image", "inspect", image], { stdio: "ignore" });
  } catch {
    return {
      ok: false,
      output: `gate: sandbox image '${image}' not found. It is built on the first agent run; build it before running the gate on a fresh clone.`,
    };
  }

  const commands = [...GATE_COMMANDS];
  if (!fs.existsSync(path.join(worktreePath, TEST_DIR, "node_modules"))) {
    commands.unshift("npm ci");
  }
  const uid = process.getuid?.() ?? 1000;
  const gid = process.getgid?.() ?? 1000;
  for (const command of commands) {
    try {
      console.log(`gate: ${command} (in ${TEST_DIR}, sandboxed)`);
      execFileSync(
        "docker",
        [
          "run", "--rm",
          "--cap-drop", "ALL",
          "--security-opt", "no-new-privileges",
          "--init",
          "--user", `${uid}:${gid}`,
          "-e", "HOME=/home/agent",
          // The image's ENTRYPOINT is `sleep infinity` (the provider execs into
          // a kept-alive container); without an override the gate command would
          // be passed to sleep as arguments and never run.
          "--entrypoint", "bash",
          "-v", `${path.resolve(worktreePath)}:${SANDBOX_WORKTREE}:z`,
          "-w", path.posix.join(SANDBOX_WORKTREE, TEST_DIR),
          image,
          "-c", command,
        ],
        {
          encoding: "utf8",
          stdio: ["ignore", "pipe", "pipe"],
          maxBuffer: 64 * 1024 * 1024,
          // On expiry Node sends SIGTERM to the docker client, which stops the
          // container so --rm can clean it up.
          timeout: GATE_TIMEOUT_SECONDS * 1000,
        },
      );
    } catch (err) {
      const e = err as {
        stdout?: string;
        stderr?: string;
        message?: string;
        killed?: boolean;
      };
      const header =
        e.killed === true
          ? `$ ${command}\ngate timed out after ${GATE_TIMEOUT_SECONDS}s`
          : `$ ${command}`;
      const output = [header, e.stdout ?? "", e.stderr ?? e.message ?? ""]
        .join("\n")
        .split("\n");
      return { ok: false, output: output.slice(-80).join("\n") };
    }
  }
  return { ok: true, output: "" };
};

const SECRET_PATTERNS = [
  /AKIA[0-9A-Z]{16}/,
  /gh[posru]_[A-Za-z0-9]{30,}/,
  /xox[baprs]-[A-Za-z0-9-]{10,}/,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/,
  /AIza[0-9A-Za-z_-]{35}/,
  /(api[_-]?key|secret|token|password|passwd|access[_-]?key)["']?\s*[:=]\s*["'][A-Za-z0-9/+_=.-]{16,}["']/i,
];

export const diffHasSecret = (worktreePath: string): boolean => {
  const diff = execSync(`git diff ${BASE}...HEAD`, {
    cwd: worktreePath,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const added = diff
    .split("\n")
    .filter((line) => line.startsWith("+") && !line.startsWith("+++"))
    .join("\n");
  return SECRET_PATTERNS.some((pattern) => pattern.test(added));
};

// The verifier's last line is `VERDICT: pass` or `VERDICT: fail — <reason>`.
// "missing" means no VERDICT line was found at all (malformed output).
export type VerifierVerdict = {
  verdict: "pass" | "fail" | "missing";
  reason: string;
};

export const parseVerifierVerdict = (text: string): VerifierVerdict => {
  const matches = [...text.matchAll(/VERDICT:\s*(pass|fail)\s*(?:[—–:-]+\s*(.*))?/gi)];
  if (matches.length === 0) return { verdict: "missing", reason: "" };
  const last = matches[matches.length - 1];
  const verdict = last[1].toLowerCase() as "pass" | "fail";
  return {
    verdict,
    reason: verdict === "fail" ? (last[2] ?? "").trim() : "",
  };
};

export const recordMetric = (
  issueNumber: number,
  outcome: string,
  cycles: number | "",
  durationSeconds: number,
): void => {
  const file = path.join(".sandcastle", "logs", "metrics.csv");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const timestamp = new Date().toISOString();
  fs.appendFileSync(
    file,
    `${timestamp},${issueNumber},${outcome},${cycles},${durationSeconds}\n`,
  );
};
