#!/usr/bin/env node

/**
 * CI invocation script for the drift detector agent.
 *
 * Determines the diff, passes it to Claude via the Agent SDK with the
 * drift-detector agent instructions as system prompt, and exits with
 * the appropriate code:
 *   0 — no drift detected
 *   1 — drift detected
 *   2 — infrastructure error
 */

import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { query } from "@anthropic-ai/claude-agent-sdk";

// --- Structured output schema ---

const driftSchema = {
  type: "object",
  properties: {
    drift_detected: { type: "boolean" },
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          file: { type: "string" },
          lines: { type: "string" },
          affected_artifact: { type: "string" },
          justification: { type: "string" },
          suggestion: { type: "string" },
        },
        required: ["file", "affected_artifact", "justification", "suggestion"],
      },
    },
    summary: { type: "string" },
  },
  required: ["drift_detected", "findings", "summary"],
};

// --- Helpers ---

function git(cmd) {
  return execSync(`git ${cmd}`, { encoding: "utf-8" }).trim();
}

function parseArgs(argv) {
  const args = { base: null, head: null };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--base" && argv[i + 1]) {
      args.base = argv[++i];
    } else if (argv[i] === "--head" && argv[i + 1]) {
      args.head = argv[++i];
    }
  }
  return args;
}

function determineDiff(cliArgs) {
  // 1. Explicit CLI refs
  if (cliArgs.base && cliArgs.head) {
    return git(`diff ${cliArgs.base}..${cliArgs.head}`);
  }

  // 2. GitLab MR pipeline — diff the MR base against current commit
  const mrBase = process.env.CI_MERGE_REQUEST_DIFF_BASE_SHA;
  const commitSha = process.env.CI_COMMIT_SHA;
  if (mrBase && commitSha) {
    return git(`diff ${mrBase}..${commitSha}`);
  }

  // 3. Branch pipeline — diff against merge base with default branch
  const branch = process.env.CI_COMMIT_BRANCH;
  const defaultBranch = process.env.CI_DEFAULT_BRANCH;
  if (branch && defaultBranch) {
    if (branch === defaultBranch) {
      // On main: diff the latest commit only
      return git("diff HEAD~1..HEAD");
    }
    // Feature branch: deepen history and fetch default branch to find merge base
    try { git("fetch --unshallow"); } catch {}
    git(`fetch origin ${defaultBranch}`);
    const mergeBase = git(`merge-base origin/${defaultBranch} HEAD`);
    return git(`diff ${mergeBase}..HEAD`);
  }

  // 4. Cannot determine — fail explicitly
  console.error(
    "Error: Cannot determine diff. Provide --base and --head, " +
      "or run in a GitLab MR/branch pipeline.",
  );
  process.exit(2);
}

function formatFindings(result) {
  if (!result.drift_detected || result.findings.length === 0) {
    console.log("No drift detected.");
    return;
  }

  for (const f of result.findings) {
    const location = f.lines ? `${f.file}:${f.lines}` : f.file;
    console.log(`DRIFT: ${location}`);
    console.log(`  Affected: ${f.affected_artifact}`);
    console.log(`  Reason: ${f.justification}`);
    console.log(`  Suggestion: ${f.suggestion}`);
    console.log();
  }

  if (result.summary) {
    console.log(result.summary);
  }
}

// --- Main ---

async function main() {
  const cliArgs = parseArgs(process.argv);
  const diff = determineDiff(cliArgs);

  if (!diff) {
    console.log("Empty diff — nothing to check.");
    process.exit(0);
  }

  // Read the agent instructions from the markdown file (strip frontmatter)
  const agentPath = join(process.cwd(), ".claude", "agents", "drift-detector.md");
  const agentRaw = readFileSync(agentPath, "utf-8");
  const agentPrompt = agentRaw.replace(/^---[\s\S]*?---\n*/, "");

  const prompt = [
    "Analyze the following diff for drift against the project's specs, ADRs, tests, and navigation aids.",
    "",
    "```diff",
    diff,
    "```",
  ].join("\n");

  let structured = null;
  let resultMessage = null;

  for await (const message of query({
    prompt,
    options: {
      systemPrompt: agentPrompt,
      allowedTools: ["Read", "Glob", "Grep", "Bash"],
      permissionMode: "acceptEdits",
      settingSources: ["project"],
      cwd: process.cwd(),
      outputFormat: {
        type: "json_schema",
        schema: driftSchema,
      },
    },
  })) {
    if (message.type === "result") {
      resultMessage = message;
      structured = message.structured_output;
    }
  }

  if (!resultMessage) {
    console.error("Error: No result received from drift detector.");
    process.exit(2);
  }

  // Try structured_output first, fall back to parsing result text
  if (!structured && resultMessage.result) {
    try {
      structured = JSON.parse(resultMessage.result);
    } catch {
      console.error("Error: Failed to parse result as JSON.");
      console.error(resultMessage.result);
      process.exit(2);
    }
  }

  if (!structured) {
    console.error("Error: No output from drift detector.");
    process.exit(2);
  }

  formatFindings(structured);
  process.exit(structured.drift_detected ? 1 : 0);
}

main().catch((err) => {
  console.error("Error: Drift detection failed.");
  console.error(err.message || err);
  if (err.stack) console.error(err.stack);
  process.exit(2);
});
