# odoo-impact-graph

Know what a change in an Odoo code base will touch **before** you make it.

`odoo-impact` reads an Odoo code base and builds a layered impact graph:

| Level | Nodes |
|---|---|
| L4 | menus, window actions, server actions, crons, automations |
| L3 | views (with all their inherited extensions), buttons, reports |
| L2 | models and wizards (TransientModel) |
| L1 | compute methods, constraints, onchanges, `write`/`create`/`copy`/`unlink` overrides, other methods |
| L0 | fields |

Then it answers four questions, from the command line, in an HTML explorer, or for an AI agent over MCP:

- **If this field changes**, which fields recompute, which constraints / onchanges / hooks run, and on which views, actions and menus the user sees it?
- **Who changes this field**: which methods write it, and which buttons, menus and crons lead there?
- **From this menu / button / method**, which fields can change?
- **This git diff / this plan** touches what, and which screens must be tested?

It is plain Python (3.8+, standard library only). No AI is involved in building or querying the graph.

## Install

```bash
pip install git+https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git
```

## Build the graph

### From source: just pick the folders (no database)

```bash
odoo-impact build /path/to/custom-addons --core /path/to/odoo --core /path/to/enterprise
```

- The positional folders are what you work on. A path can be an addons folder, a project folder that contains addons, a single module, or an Odoo source tree (the folder with `odoo-bin`).
- `--core` adds folders that are only used to resolve fields: the Odoo source, Enterprise, OCA repositories.
- A module counts as **custom** unless it is inside an Odoo source tree, has license `OEEL-1`, or comes from a `--core` folder. Use `--custom FOLDER` to choose explicitly.
- The modules taken into account are the custom ones plus their dependencies plus the `auto_install` modules they enable, like an installation. Use `--modules a,b` to choose them explicitly.
- By default the graph keeps what is connected to custom code. Use `--all` to keep everything.
- Run `odoo-impact build` without a folder and it asks for one.

The output is `.odoo-impact/graph.json` plus `.odoo-impact/graph.html`. Change the location with `-o`.

The source scanner follows Odoo's own rules:
- Classes with the same `_name` / `_inherit` are merged in module load order.
- `_inherit` of a mixin copies its fields, and `_inherits` delegates them.
- A computed field depends on the union of the `@api.depends` of every override of its method, including `@api.depends(*CONSTANT)`.
- Every prefix of a dependency path triggers the field, and so does the inverse many2one of each one2many on the path.
- A wizard is never triggered by a regular model.
- A view is its arch plus every extension view that inherits it, and fields inside a one2many sub-view belong to the line model.

Source mode was run on Odoo 17, 18 and 19 trees. On a 51-custom-module Odoo 17 code base, the field dependency edges found from source matched the edges of the running registry with 99.7% recall and 99.9% precision.

### From a database registry (exact)

```bash
odoo-impact build-registry --odoo-bin /path/to/odoo/odoo-bin --python /path/to/odoo-venv/bin/python \
    -c /path/to/odoo.conf -d my_database
```

This runs `odoo-bin shell` read-only, with the script in `odoo_impact_graph/registry_dump.py` on stdin. It takes the field triggers, constraints and onchanges straight from the registry, the views from their combined arch, and the actions, menus, crons and automations from the database. Method bodies of the custom modules are then read from source. Tested on Odoo 17; the dump falls back to the older API names for Odoo 14–16.

## Ask questions

```bash
odoo-impact find sale order amount
odoo-impact field sale.order.amount_total          # if it changes → what follows + screens
odoo-impact who sale.order.line.price_unit         # what can change it
odoo-impact from "Sales/Orders/Quotations"          # what a menu / action / button / method can change
odoo-impact plan sale.order.state sale.order::action_confirm   # before coding
odoo-impact diff origin/main --repo .              # impact of your branch (committed + uncommitted)
odoo-impact node sale.order::write                 # one node, its files and direct edges
```

Ids are `model.field` for fields and `model::method` for methods. Views, actions and menus use their xmlid (`view:sale.view_order_form`, `action:…`, `menu:…`). Labels like `"Sales/Orders/Quotations"` also work.

Options:
- `--depth N` sets how many hops to follow (default 6).
- `--core-logic` also shows methods of core modules.
- `-g FILE` uses another graph.

### Edge sources

| Mark | Source | Trust |
|---|---|---|
| (none) | `registry`: the running Odoo registry | exact |
| (none) | `static`: declarations (fields, `@api.*`, views, actions, menus) | reliable |
| `~` | `ast`: method bodies (`'x' in vals`, `vals.get('x')`, `.write({...})`, `self.env['m'].create({...})`, `rec.x = ...`, `self._method()` calls, `res_model` / `env.ref` that open something) | heuristic, check the `file:line` |

## HTML explorer

`graph.html` opens from disk; the graph is embedded in the file. Search a node, then pick a direction:
- **If it changes →**
- **← Who changes it**
- **From here ↓**

The result shows as five lanes, from L4 (what the user clicks) down to L0 (fields). Hover a card to light up its path to the starting point. Double-click a card to start from it. The side panel shows files, edges and the exact path.

Regenerate the page with `odoo-impact html -g graph.json`.

## MCP server for AI agents

```bash
odoo-impact mcp -g /abs/path/.odoo-impact/graph.json
```

This is a stdio JSON-RPC MCP server with no dependencies. It reloads the graph whenever the file is rebuilt. It provides these tools:

| Tool | Use |
|---|---|
| `impact_find` | search ids |
| `impact_of_change` | if this field/method changes |
| `impact_who_changes` | what writes a field |
| `impact_from_entry` | what a menu/action/button/method can change |
| `impact_plan_change` | **before writing code**: fields that follow, logic that runs, existing writers, screens to test |
| `impact_diff` | impact of the changed lines in a git repo |
| `impact_node` | details of one node |
| `impact_graph_info` | what the graph was built from |

Claude Code:

```bash
claude mcp add odoo-impact -- odoo-impact mcp -g /abs/path/.odoo-impact/graph.json
```

Any MCP client (`mcpServers` JSON):

```json
{
  "mcpServers": {
    "odoo-impact": {
      "command": "odoo-impact",
      "args": ["mcp", "-g", "/abs/path/.odoo-impact/graph.json"]
    }
  }
}
```

A good instruction for the agent: *"Before changing Odoo code, call `impact_plan_change` with every field and method you will touch; after editing, call `impact_diff`."*

## Limits

- **Source mode cannot know what is installed.** It guesses from dependencies, so modules installed for other reasons are missing until you pass them with `--modules`.
- **XPath is not applied.** Views are combined, but a field removed by `position="replace"` still counts as shown.
- **one2many `domain` dependencies are not added.** Odoo adds the fields used in a one2many `domain` as dependencies; the source scanner does not.
- **Method-body edges (`~`) are pattern-based.** A write through a variable that holds a model (`Order = self.env['sale.order']; Order.create(...)`) is not seen.
- **`diff` maps Python lines only.** XML-only changes are not mapped yet.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e .[test]
.venv/bin/python -m pytest
```

`tests/fixtures` holds two tiny addons (a core `shop` and a custom `shop_discount`) that cover compute chains through one2many, related fields, an extension view, a wizard, a cron and a write hook.

## License

MIT
