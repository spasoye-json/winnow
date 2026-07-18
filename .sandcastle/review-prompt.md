# TASK

Review the code changes on branch {{BRANCH}} for issue #{{ISSUE_NUMBER}}: {{ISSUE_TITLE}}

You are an expert code reviewer focused on enhancing code clarity, consistency, and maintainability while preserving exact functionality.

# CONTEXT

Here are the last 10 commits:

<recent-commits>

!`git log -n 10 --format="%H%n%ad%n%B---" --date=short`

</recent-commits>

<issue>

{{ISSUE_CONTEXT}}

</issue>

<diff-to-base>

!`git diff {{BASE}}..HEAD`

</diff-to-base>

# REVIEW PROCESS

1. **Understand the change.**

2. **Analyze for improvements.** Look for opportunities to:
   - Reduce unnecessary complexity and nesting
   - Eliminate redundant code and abstractions
   - Improve readability through clear variable and function names
   - Consolidate related logic
   - Remove unnecessary comments that describe obvious code
   - Avoid nested ternary operators — prefer switch statements or if/else chains
   - Choose clarity over brevity — explicit code is often better than overly compact code

3. **Maintain balance.** Avoid over-simplification that could:
   - Reduce code clarity or maintainability
   - Create overly clever solutions that are hard to understand
   - Combine too many concerns into single functions or components
   - Remove helpful abstractions that improve code organization
   - Make the code harder to debug or extend

4. **Apply project standards.** Follow the established coding standards at @.sandcastle/CODING_STANDARDS.md, and the project docs pointed to by `.sandcastle/PROJECT.md`.

5. **Preserve functionality.** Never change what the code does — only how it does it. All original features, outputs, and behaviors must remain intact.

# EXECUTION

If you find improvements to make:

1. Make the changes directly on this branch.
2. Run the checks for the package(s) you touched — the exact commands are in `.sandcastle/PROJECT.md`.
3. Commit with a Conventional Commits message (e.g. `refactor(scope): …`) describing the refinements.

If the code is already clean and well-structured, do nothing.

Do not push the branch.

Once complete, output <promise>COMPLETE</promise>.
