# odoo-impact-graph

Know what a change in an Odoo code base will touch **before** you make it, and let your coding agent know it too.

`odoo-impact` reads an Odoo code base and builds a layered impact graph:

| Level | Nodes |
|---|---|
| L4 | menus, window actions, server actions, crons, automations |
| L3 | views (combined with every inherited extension), buttons, reports |
| L2 | models and wizards (TransientModel) |
| L1 | compute methods, constraints, onchanges, `write` / `create` / `copy` / `unlink` overrides, other methods |
| L0 | fields |

It runs as a **local web service**. The dashboard lists every git worktree of your project; press **Generate** on a worktree to build its graph, then open it, ask it questions, or see what the branch changes.

It answers these questions:
- **If this field or method changes:** which fields recompute, which constraints, onchanges and hooks run, and on which views, actions and menus the user sees it.
- **Who changes this field:** which methods write it, and which buttons, menus and crons lead there.
- **From this menu, button or method:** which fields can change.
- **This plan, or this git diff:** what it touches, and which screens to retest.

You can ask in three ways:
- a **CLI**;
- a **web explorer**;
- an **MCP server**, so an AI coding agent can ask before it edits.

**Agent skills** in [`.agents/skills`](.agents/skills) plug the graph into the usual Odoo development skills.

Building and querying the graph is plain Python 3.8+ (standard library only). No AI is used, and you don't need Odoo installed or a database.

---

## 1. Install

You need **Python 3.8 or newer** and **git**. You do **not** need Odoo's Python dependencies or a database.

Choose one way to install.

**A. pipx (recommended).** Isolated, and puts `odoo-impact` on your PATH:

```bash
pipx install git+https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git
```

**B. A dedicated virtualenv:**

```bash
python3 -m venv ~/.local/share/odoo-impact/venv
~/.local/share/odoo-impact/venv/bin/pip install git+https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git
mkdir -p ~/.local/bin && ln -sf ~/.local/share/odoo-impact/venv/bin/odoo-impact ~/.local/bin/odoo-impact
```

**C. From a clone (to hack on it):**

```bash
git clone https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git
cd odoo-impact-graph
python3 -m venv .venv && .venv/bin/pip install -e .[test]
```

Check it works:

```bash
odoo-impact --version
```

To upgrade, use `pipx upgrade odoo-impact-graph`, or run the same `pip install` again with `-U`.

## 2. Build the graph of your project

Run these from your project root. The graph is written to `.odoo-impact/graph.json`, with `.odoo-impact/graph.html` next to it.

**Easiest: point at the Odoo config you run with.** The command uses the config's `addons_path`:

```bash
odoo-impact build --from-conf /path/to/odoo.conf
```

**Or pick the folders yourself:**

```bash
odoo-impact build /path/to/custom-addons --core /path/to/odoo --core /path/to/enterprise
```

Or run `odoo-impact build` with no arguments, and it asks for a folder.

A folder can be:
- an addons folder;
- a project folder that contains addons;
- a single module;
- an Odoo source tree (the folder with `odoo-bin`).

**Which modules count as custom.** A module counts as **custom** unless:
- it sits in an Odoo source tree,
- it has license `OEEL-1` (Enterprise), or
- it comes from a `--core` folder.

To decide yourself, pass `--custom FOLDER` (repeatable).

**Which modules are taken into account.** The build takes the custom modules, their dependencies, and the `auto_install` modules they enable, like an install would. To list them yourself, pass `--modules a,b`.

**What the graph keeps.** The graph keeps what is connected to custom code. Pass `--all` to keep everything.

Read the summary line, `N modules found, M selected (K custom)`:

| What you see | Fix |
|---|---|
| `0 custom` | add `--custom /path/to/custom-addons` |
| a module you use is missing | add `--modules that_module` |
| many "could not be resolved" `@api.depends` | a dependency folder is not scanned; add it with `--core` |

**After a branch switch, a pull or edits**, rebuild with the same options (they are recorded in the graph):

```bash
odoo-impact rebuild
```

**Keep the graph out of git.** It contains absolute paths:

```bash
echo '.odoo-impact/' >> .git/info/exclude
```

### Exact variant: from a database registry

If you have a **local** database of the project, the graph can come from Odoo's running registry instead of from source. This mode is exact for field dependencies and views, and it uses a read-only `odoo-bin shell`. Never point it at production.

```bash
odoo-impact build-registry --odoo-bin /path/to/odoo/odoo-bin --python /path/to/odoo-venv/bin/python \
    -c /path/to/odoo.conf -d my_local_db
```

The method bodies of custom modules are still read from source. Registry mode is tested on Odoo 17, and falls back to older API names for Odoo 14–16.

### How the source build follows Odoo

- Classes with the same `_name` / `_inherit` merge in module load order.
- A mixin's `_inherit` copies its fields; `_inherits` delegates them.
- A compute depends on the union of `@api.depends` of every override, including `@api.depends(*CONSTANT)`.
- Every prefix of a dependency path triggers the field, and so does the inverse many2one of each one2many on the path.
- A wizard is never triggered by a regular model.
- A view is its arch plus all extension views, and fields inside a one2many sub-view belong to the line model.

Source mode was run on Odoo 17, 18 and 19. On a 51-custom-module Odoo 17 code base, its field dependency edges matched the running registry with **99.7% recall and 99.9% precision**.

## 3. Ask questions (CLI)

```bash
odoo-impact find sale order amount                 # get exact ids
odoo-impact field sale.order.amount_total          # if it changes: what follows, which screens show it
odoo-impact who sale.order.line.price_unit         # what can change it
odoo-impact from "Sales/Orders/Quotations"          # what a menu / action / button / method can change
odoo-impact plan sale.order.state sale.order::action_confirm   # before coding
odoo-impact diff origin/main                       # after coding: committed + uncommitted changes vs a branch
odoo-impact node sale.order::write                 # one node: files, every override, direct edges
```

**Ids:**

| Kind | Format | Example |
|---|---|---|
| field | `model.field` | `sale.order.amount_total` |
| method | `model::method` | `sale.order::action_confirm` |
| view | `view:<xmlid>` | `view:sale.view_order_form` |
| action | `action:<xmlid>` | `action:sale.action_quotations` |
| menu | `menu:<xmlid>` | `menu:sale.menu_sale_quotations` |

A label works too, for example `"Sales/Orders/Quotations"`.

**Options:**
- `--depth N`: how many hops to follow (default 6).
- `--core-logic`: also show methods of core modules.
- `-g FILE`: use another graph. You can also set the `ODOO_IMPACT_GRAPH` environment variable.

**How far to trust each edge:**

| Mark | From | Trust |
|---|---|---|
| (none) | the running registry, or declarations (fields, `@api.*`, views, actions, menus) | exact / reliable |
| `~` | method bodies: `'x' in vals`, `vals.get('x')`, `.write({...})`, `self.env['m'].create({...})`, `rec.x = ...`, calls, `res_model` / `env.ref` | heuristic; check the `file:line` |

## 4. Web service: dashboard with one-click Generate per worktree

Run it once as a service. It stays on **http://127.0.0.1:8770/**.

```bash
odoo-impact service install        # systemd --user unit, starts now and at login (Linux, WSL with systemd)
odoo-impact service status
# or in a terminal, without systemd:
odoo-impact serve --open
```

In the dashboard:

1. **Add project.** Give the git repository of your custom addons and its `odoo.conf`, or the Odoo / Enterprise folders. In the conf, folders inside the repository are swapped for the worktree being built; the other folders are core.
2. **See every git worktree of the repository**, with its branch, last commit and graph state:

   | State | Meaning |
   |---|---|
   | no graph | never generated |
   | up to date | matches the current commit |
   | outdated | new commits, or uncommitted changes, since the last build |

3. **Generate / Regenerate** builds the graph of that worktree in the background, in about 5–10 s for ~200 modules.
4. **Open** shows the explorer of that worktree.
5. **Changes** lists what the worktree's commits and uncommitted edits touch, compared with `HEAD` or any branch (`origin/main`, `preprod`…), and the screens to retest.
6. **Ask** answers if it changes / who changes it / from here, as text.

Without a browser:

```bash
odoo-impact project add /path/to/repo --conf /path/to/odoo.conf   # register
odoo-impact project list                                          # worktrees and graph state
odoo-impact project build /path/to/a/worktree                     # generate one
```

Service data lives in `~/.local/share/odoo-impact` (`ODOO_IMPACT_HOME`): `projects.json`, plus one `graphs/<id>/graph.json` per worktree.

The service listens on 127.0.0.1 only. It refuses foreign `Host` headers (DNS rebinding), and it only starts builds on requests that carry its own header, which a web page on another site cannot send. To reach it from another machine, use an SSH tunnel; don't expose it.

## 5. One graph without the service (web view)

```bash
odoo-impact web
```

This serves the explorer on **http://127.0.0.1:8765/** and opens your browser. Stop it with Ctrl+C.

| Need | Command |
|---|---|
| another port, or any free one | `odoo-impact web --port 9000` / `--port 0` |
| headless / remote shell: only print the URL | `odoo-impact web --no-open` |
| another graph | `odoo-impact web -g /abs/project/.odoo-impact/graph.json` |
| no server at all | open `.odoo-impact/graph.html` straight from disk |
| WSL | it opens through `wslview` if installed; otherwise paste the URL in the Windows browser |
| remote machine | keep the default host and tunnel: `ssh -L 8765:127.0.0.1:8765 server`. Don't expose the page publicly: it lists your code structure. |

**Using the page:**
- **Search** a field, method, view, button or menu.
- **Pick a direction:** **If it changes →**, **← Who changes it**, or **From here ↓**.
- **Read the lanes.** The result shows as five lanes, from L4 (what the user clicks) at the top down to L0 (fields).
- **Hover** a card to light up its path back to the start.
- **Click** a card to see its files (`file:line`), every edge and the exact path.
- **Double-click** a card to start from it.
- Use **Depth** and **core logic** to follow further.

**Deep links** open a question directly:

```
http://127.0.0.1:8765/#impact|sale.order.amount_total
http://127.0.0.1:8765/#who|sale.order.line.price_unit
http://127.0.0.1:8765/#from|menu:sale.menu_sale_quotations
```

## 6. Connect your AI coding agent (MCP)

This is a stdio MCP server with no dependencies. It reloads the graph whenever it is rebuilt. There are two ways to run it:

```bash
odoo-impact mcp --auto                                   # with the service: the graph of the worktree the agent runs in
odoo-impact mcp -g /abs/project/.odoo-impact/graph.json  # one fixed graph file
```

**`--auto` is the recommended mode.** It needs the project to be registered in the service. Each agent session then gets the graph of its own worktree, so one registration covers every branch. If the worktree has no graph yet, the `impact_rebuild` tool builds it.

**Register it:**

```bash
# Claude Code, once for all projects
claude mcp add odoo-impact -s user -- odoo-impact mcp --auto
claude mcp get odoo-impact      # Status: ✔ Connected
# or one fixed graph, private to the current project
claude mcp add odoo-impact -s local -- odoo-impact mcp -g "$PWD/.odoo-impact/graph.json"
```

```toml
# Codex: ~/.codex/config.toml
[mcp_servers.odoo-impact]
command = "odoo-impact"
args = ["mcp", "--auto"]          # or ["mcp", "-g", "/abs/project/.odoo-impact/graph.json"]
```

```json
// Cursor, Claude Desktop and other "mcpServers" clients
{"mcpServers": {"odoo-impact": {"command": "odoo-impact", "args": ["mcp", "--auto"]}}}
```

If the agent does not see `odoo-impact` on its PATH, use the absolute path from `which odoo-impact`. The tools appear in the next agent session.

| Tool | Use |
|---|---|
| `impact_find` | search ids |
| `impact_plan_change` | **before writing code**: fields that follow, logic that runs, existing writers, screens to test |
| `impact_of_change` | if this field / method changes |
| `impact_who_changes` | what writes a field |
| `impact_from_entry` | what a menu / action / button / method can change |
| `impact_rebuild` | rebuild the graph after edits or a branch switch |
| `impact_diff` | impact of the changed lines in a git repo |
| `impact_node` | one node, its files and direct edges |
| `impact_graph_info` | what the graph was built from, and when |

## 7. Install the agent skills

The [`.agents/skills`](.agents/skills) folder holds four skills, in the common `SKILL.md` format used by Claude Code, Codex and other agents:

| Skill | What it gives the agent |
|---|---|
| [`odoo-impact-setup`](.agents/skills/odoo-impact-setup/SKILL.md) | installs the CLI, builds the project graph, registers the MCP server, installs the skills |
| [`odoo-impact`](.agents/skills/odoo-impact/SKILL.md) | how to ask the graph: which tool for which question, how to read levels and `~` edges, the plan → edit → diff loop |
| [`odoo-impact-web`](.agents/skills/odoo-impact-web/SKILL.md) | opens and explains the web explorer, deep links, screenshots |
| [`odoo-impact-dev-workflow`](.agents/skills/odoo-impact-dev-workflow/SKILL.md) | connects the graph to the other development skills (below) |

`odoo-impact-dev-workflow` does not replace your development skills. It tells the agent at which step of each one to ask the graph, and what to do with the answer:

| Kind of work | Example skills |
|---|---|
| planning | `spec-driven-development`, `planning-and-task-breakdown` |
| models / views / wizards | `odoo17-new-model`, `odoo17-wizard-builder`, `odoo-guidelines` |
| TDD | `tdd` |
| edge-case and exploratory testing | `odoo17-edge-case-tests`, `odoo17-exploratory-chaos-testing` |
| manual test cases | `odoo17-manual-testcase-generator` |
| debugging | `diagnose` |
| review | `odoo-review`, `code-review` |
| cherry-pick / release | `odoo-cherry-pick-guard` |

It also gives a short impact block for PR descriptions.

**Install the skills:**

```bash
git clone https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git ~/src/odoo-impact-graph
~/src/odoo-impact-graph/scripts/install-skills.sh                      # ~/.agents/skills (or ~/.claude/skills)
~/src/odoo-impact-graph/scripts/install-skills.sh --link               # symlinks: `git pull` updates them
~/src/odoo-impact-graph/scripts/install-skills.sh --target ./.agents/skills   # only for one project
```

Restart the agent afterwards. Then ask it to *"set up odoo-impact for this project"*, and the `odoo-impact-setup` skill does steps 2 to 5 for you.

## Quick start (all steps)

```bash
pipx install git+https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git
odoo-impact service install                                            # dashboard on http://127.0.0.1:8770/
odoo-impact project add /path/to/custom-addons-repo --conf /path/to/odoo.conf
odoo-impact project build /path/to/custom-addons-repo                  # or press Generate in the dashboard
claude mcp add odoo-impact -s user -- odoo-impact mcp --auto           # the agent sees the graph of its worktree
git clone https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git ~/src/odoo-impact-graph
~/src/odoo-impact-graph/scripts/install-skills.sh                      # skills for the agent
```

Without the service, a single project folder works too: `odoo-impact build --from-conf odoo.conf`, `odoo-impact web`.

## Limits

- **Installed modules are guessed in source mode.** The guess comes from dependencies; add the rest with `--modules`, or use `build-registry`.
- **XPath is not applied.** A field removed by `position="replace"` still counts as shown.
- **one2many `domain` dependencies are not added.** Odoo adds them; the source build does not.
- **Method-body edges (`~`) are pattern-based.** A write through a variable that holds a model (`Order = self.env['sale.order']; Order.create(...)`) is not seen.
- **`diff` maps Python lines only.** For XML-only changes, query the changed view or button with `node`.

## Development

```bash
.venv/bin/python -m pytest
```

`tests/fixtures` holds two tiny addons, a core `shop` and a custom `shop_discount`. They cover:
- compute chains through a one2many;
- related fields;
- an extension view;
- a wizard;
- a cron;
- a write hook;
- conf / rebuild;
- the web server and the MCP protocol.

## License

MIT
