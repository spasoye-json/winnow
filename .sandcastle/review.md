# Independent code-quality review

A change has been implemented on this branch. Review it with **fresh eyes** — you did not write it,
and your job is to be a strict, independent reviewer (think Greptile / CodeRabbit, but harsher).

## How to review

1. Look at exactly what changed on this branch and nothing else:
   - `git diff {{BASE}}...HEAD` for the diff
   - inspect the changed files in full where you need context

2. Perform a **thermo-nuclear code-quality review** of *only this branch's changes*: abstraction
   quality, oversized files, spaghetti conditionals, duplication, maintainability, and "code judo"
   restructurings that preserve behaviour while making the code simpler. Be ambitious and rigorous.

3. **Do not edit any files.** You only produce a verdict.

## Output

Emit your verdict as the **last** thing you print, as a single JSON block inside `<verdict>` tags:

<verdict>
{"verdict": "approve", "findings": []}
</verdict>

- Use `"approve"` only when there are no material maintainability problems with the change.
- Otherwise use `"request_changes"` and list concrete, actionable findings in `findings` (one string
  per problem, each naming the file and the specific fix).
