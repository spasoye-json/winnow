# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

## Sandcastle agent labels

The sandcastle runners (`.sandcastle/`, `.github/workflows/agent-*.yml`) add their own labels on top of the triage vocabulary.

Local runners (`npm run agent`, `npm run agent:parallel`) drain the `ready-for-agent` queue:

| Label | Meaning |
| ----- | ------- |
| `claude-working` | A local runner has claimed this issue and is working it |
| `ready-for-human` | Runner gave up (gate red or blocked); needs a human |

GitHub Actions runners trigger on applying a command label to an issue or PR:

| Label | Triggers |
| ----- | -------- |
| `agent:explore` | Context-map exploration of an issue, posted as a comment |
| `agent:implement` | Implement the issue on a branch and open a PR |
| `agent:review` | Review the PR (also auto-applied by the implement flow) |
| `agent:update-branch` | Resolve a PR's conflicts with the base branch |

State labels the Actions flows manage themselves: `agent:in-progress` while a flow runs, `agent:blocked` when a flow refuses or fails.
