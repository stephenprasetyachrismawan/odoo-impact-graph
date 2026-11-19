---
name: odoo-impact-dev-workflow
description: Makes every Odoo development skill impact-aware - tells the agent at which step of planning, model/view/wizard building, refactoring, TDD, edge-case or exploratory testing, manual test cases, debugging, code review and cherry-pick/release to call the odoo-impact graph, and what to do with the answer. Use at the start of any Odoo addon development, bug fix, refactor, test or review task in a project that has an odoo-impact graph, and whenever another Odoo skill is about to change fields, computes, write/create overrides, views, buttons, wizards or crons.
---

# Impact-aware Odoo development

Odoo code is fragile where one change silently moves another: a compute chain through a one2many,
a `write()` override that reacts to a key, a second writer of the same field, a view that shows the
value on a screen nobody retests. The impact graph makes those links visible. This skill does not
replace the other development skills; it adds one impact step to each of them.

Prerequisite: an `odoo-impact` graph for the project (MCP `impact_*` tools or the `odoo-impact` CLI).
Missing → `odoo-impact-setup`. How to read answers → `odoo-impact`. Visual → `odoo-impact-web`.

## The three moments

1. **Before the first edit**: `impact_plan_change` with every field and method you intend to touch.
2. **While designing tests / checks**: `impact_who_changes` and `impact_of_change` on the fields at the
   heart of the rule.
3. **Before declaring done**: `impact_rebuild` + `impact_diff` against the base branch; compare with the plan.

Write the result into the work, not only into the chat: plan notes, test list, PR / merge summary.

## Hook into the skill in use

Use the row matching the skill (or kind of work) that is running. Skill names are the common ones;
match by purpose when the project uses others.

| Work / skill | When | Call | Use the answer to |
|---|---|---|---|
| Spec, PRD, planning, task breakdown (`spec-driven-development`, `planning-and-task-breakdown`, `to-prd`, `grill-me`) | listing scope | `impact_plan_change` on the fields/methods named in the requirement | add one task per writer that must follow the new rule and one per screen to retest; surface questions about side effects the user did not mention |
| Architecture / new model (`senior-odoo-17-architect`, `odoo17-new-model`, `odoo17-model-adaptive-upgrader`) | before choosing `_inherit` vs new model, stored vs not | `impact_find` for similar fields; `impact_who_changes` on fields to reuse | reuse an existing field instead of adding a twin; see which computes depend on a field before changing its store / type |
| Views, wizards, buttons, server actions (`odoo17-wizard-builder`, `odoo17-list-view-builder`, `odoo17-server-action`, `odoo-guidelines`) | before adding a button or a field to a view | `impact_from_entry` on the button / action you mirror; `impact_of_change` on fields you expose editable | know what the new entry point writes, and which constraints / hooks fire when users edit the exposed field |
| Editing a `write` / `create` override or a compute (`incremental-implementation`, any implementation skill) | before the edit | `impact_node` on `model::write` (all overrides and the keys they react to) and `impact_plan_change` | change the one override that owns the behaviour; keep `@api.depends` complete (the plan lists what already depends on it) |
| TDD (`tdd`, `test-driven-development`) | choosing the next behaviour to test | `impact_who_changes` on the field under test | one test per real writer path (button, wizard, cron, import) instead of only the happy path; find the existing tests to replace by grepping the writer names |
| Edge-case tests (`odoo17-edge-case-tests`) | mapping the override chain and ORM metadata | `impact_node` on the method, `impact_of_change` on the field | feed its dependency / override map: computes, constraints, hooks and every writer are the edge surface |
| Exploratory / chaos testing (`odoo17-exploratory-chaos-testing`) | mapping the workflow graph and side doors | `impact_who_changes` on state / key fields; `impact_from_entry` on menus | side doors = writers that are not the main button (crons, server actions, other models' methods) |
| Manual test cases / QA (`odoo17-manual-testcase-generator`, `qa`) | listing screens | "Screens to test" from `impact_plan_change` or `impact_diff` | one case per screen and per entry point; include screens of other modules that show the value |
| Debugging (`diagnose`, `debugging-and-error-recovery`) | "who changed this value?" | `impact_who_changes` on the wrong field, then `impact_node` on each candidate | turn the candidate list into hypotheses; confirm each by the `file:line` and data, never by the graph alone |
| Review (`odoo-review`, `code-review-and-quality`, `review`, `code-review`) | start of the review | `impact_rebuild` + `impact_diff <base>` | check every touched node and screen is intended; ask for tests on unplanned effects; add the screen list to the review |
| Cherry-pick / release (`odoo-cherry-pick-guard`, `release-to-master`) | after the picks | `impact_diff <target branch>` (graph built on the release branch) | retest list for the release; spot a picked commit whose effect depends on an unpicked one |
| Performance | before adding `depends` or `store=True` | `impact_of_change` on the dependency | a long fan-out (many fields / models) means mass recomputes on write |

## Rules

- The graph points, the code decides: confirm `~` edges and any claim you make in a root-cause
  answer at the `file:line`.
- Rebuild after switching branch or pulling (`impact_rebuild` / `odoo-impact rebuild`), else the
  answers describe another code base.
- Keep the plan small: if `impact_plan_change` returns a screen list the user would not expect, show
  it and ask before coding.
- Never run builds against a production database; `build-registry` is for local copies only.

## Summary block for PR / handover

```
Impact (odoo-impact, base <branch>):
- Touched: <fields / methods>
- Follows: <recomputed fields, hooks, constraints>
- Other writers kept consistent: <methods / buttons / crons>
- Screens to test: <menus / actions / views>
- Unplanned effects: <none | list + why accepted>
```
