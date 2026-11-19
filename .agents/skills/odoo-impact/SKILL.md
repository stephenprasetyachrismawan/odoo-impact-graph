---
name: odoo-impact
description: Answers "what does this Odoo change touch" from the project's impact graph - which fields recompute, which constraints / onchanges / write-create hooks run, which methods / buttons / menus / crons write a field, and which views, actions and menus show the effect (levels L0 field to L4 menu). Use before changing a field, compute, method, view, button or wizard in an Odoo addon, when tracing why a value changed, when listing screens to test, and when the user asks "kalau field ini diubah efeknya ke mana", "siapa yang mengubah field ini", "fitur ini menyentuh apa", impact analysis, blast radius or dependencies of a field.
---

# Odoo impact analysis

With the service, the MCP server (`odoo-impact mcp --auto`) answers from the graph of the git worktree
the session runs in, and the dashboard http://127.0.0.1:8770/ shows every worktree. Without it, the graph
is `<project>/.odoo-impact/graph.json` (or the `-g` given to the MCP server).
`impact_*` tools say "No impact graph yet" → call `impact_rebuild` once. No `odoo-impact` command, or the
worktree's repository is not registered → run the `odoo-impact-setup` skill first.

## Levels and ids

| Level | Nodes | Id |
|---|---|---|
| L4 | menu, window action, server action, cron, automation | `menu:<xmlid>`, `action:<xmlid>`, `cron:<xmlid>` |
| L3 | view (combined with its extensions), button, report | `view:<xmlid>`, `button:...` |
| L2 | model, wizard | `model:<name>` |
| L1 | compute, constraint, onchange, hook (`write`/`create`/`copy`/`unlink`), method | `model::method` |
| L0 | field | `model.field` |

Edges go "A changes or opens → B is affected or runs". Edges printed with `~` were read from
method bodies (`'x' in vals`, `.write({...})`, `rec.x = ...`, calls) and are heuristic: open the
`file:line` before stating them as fact. Edges without `~` come from declarations or the registry.

## Which question, which tool

Prefer the MCP tools when they are available; the CLI gives the same text.

| Question | MCP tool | CLI |
|---|---|---|
| Exact id of something | `impact_find` | `odoo-impact find <words>` |
| If this field / method changes, what follows and where the user sees it | `impact_of_change` | `odoo-impact field <id>` |
| What can change this field | `impact_who_changes` | `odoo-impact who <model.field>` |
| What this menu / action / button / method can change | `impact_from_entry` | `odoo-impact from <id or label>` |
| Before coding: everything a planned change touches | `impact_plan_change` | `odoo-impact plan <id> <id> ...` |
| After coding: impact of the changed lines | `impact_rebuild` then `impact_diff` | `odoo-impact rebuild && odoo-impact diff <base>`, or dashboard **Changes** |
| One node, its files and direct edges | `impact_node` | `odoo-impact node <id>` |

Defaults: depth 6, core (non-custom) methods hidden. Raise `depth` or set `core_logic` only when
the answer stops at a core method you need to see through. Lower `depth` when output is cut.

## Loop for a change

1. **Plan**: list the fields and methods the change will touch (use `impact_find` for ids), call
   `impact_plan_change` with all of them. Read its five blocks:
   - *Fields that follow*: recomputed values the change also moves; decide for each if that is wanted.
   - *Logic that runs*: constraints, onchanges, hooks; each one is a place the change can be refused or
     doubled.
   - *Code that already writes these fields*: every writer must stay consistent with the new rule.
     A rule enforced in only one writer is the usual "fragile" bug.
   - *Screens to test*: the views, actions and menus to open in the manual / QA check.
2. **Edit** at the one concrete place the behaviour happens.
3. **Check**: `impact_rebuild`, then `impact_diff` with base `HEAD` (uncommitted) or the target branch.
   A touched node or a screen that is not in the plan = an unplanned side effect: explain it or undo it.
4. **Report**: put the "Screens to test" list and any unplanned effect in the summary / PR text.

## Limits to keep in mind

- Source-built graphs guess installed modules from dependencies; a registry-built graph
  (`meta.source = registry` in `impact_graph_info`) is exact for fields and views.
- XPath `replace` is not applied (a removed field still counts as shown); one2many `domain`
  dependencies are not added; writes through a variable holding a model are not seen.
- `diff` maps Python lines; for XML-only changes, query the changed views / buttons with `impact_node`.
- The graph is as fresh as its last build: rebuild after a branch switch or a pull.
