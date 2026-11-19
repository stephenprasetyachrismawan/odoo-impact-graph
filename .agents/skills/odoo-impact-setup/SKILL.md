---
name: odoo-impact-setup
description: Installs odoo-impact-graph for an Odoo project - the odoo-impact CLI, the impact graph of the project's addons (from source or from a database registry), the MCP server for the coding agent, and the odoo-impact skills. Use when the user says "install odoo-impact", "pasang impact graph", "setup impact graph", "register the impact MCP", when an odoo-impact command or impact_* MCP tool is missing, or when another odoo-impact skill finds no graph for the project.
---

# Set up odoo-impact-graph

Goal: one graph per Odoo project at `<project>/.odoo-impact/graph.json`, an `odoo-impact` MCP server
pointing at it, and the odoo-impact skills installed. Nothing here writes to a database.

Copy and track:

```
- [ ] 1. CLI installed
- [ ] 2. Folders known (odoo.conf addons_path or explicit folders)
- [ ] 3. Graph built and smoke-tested
- [ ] 4. Graph kept out of git
- [ ] 5. MCP server registered for the agent
- [ ] 6. Skills installed
```

## 1. Install the CLI

Check first: `odoo-impact --version`. If missing, show the user the install command below and run it
only after they approve. Install only from the official repository; it needs Python 3.8+, uses the
standard library only and does not need Odoo:

```bash
pipx install git+https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git
# without pipx:
python3 -m venv ~/.local/share/odoo-impact/venv
~/.local/share/odoo-impact/venv/bin/pip install git+https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git
ln -sf ~/.local/share/odoo-impact/venv/bin/odoo-impact ~/.local/bin/odoo-impact
```

Upgrade later with the same command (`pipx upgrade odoo-impact-graph` / `pip install -U git+...`).

## 2. Find the folders

Prefer the project's Odoo config, it lists exactly the addons Odoo loads:

- the `--config=` / `-c` of the debugger or service the user runs (`.vscode/launch.json`, systemd unit,
  `docker-compose.yml`), otherwise a `*.conf` / `*.cfg` with `addons_path =` near the project.
- No config: the user's custom addons folder(s) + the Odoo source tree (folder with `odoo-bin`)
  + Enterprise / OCA folders if used.

Ask the user only when several configs match and nothing says which one is in use.

## 3. Build

Run from the project root (the graph lands in `.odoo-impact/`):

```bash
odoo-impact build --from-conf /path/to/odoo.conf
# or explicit folders
odoo-impact build /path/to/custom-addons --core /path/to/odoo --core /path/to/enterprise
```

Read the summary line: `N modules found, M selected (K custom)`. Fix before going on:

- `0 custom` → custom folders were taken as core: pass `--custom /path/to/custom-addons`.
- Too few selected → modules installed for other reasons are missing: `--modules mod_a,mod_b`.
- Many `@api.depends paths could not be resolved` → a dependency folder is not scanned: add it with `--core`.

Exact variant when a local database is available (read-only `odoo-bin shell`; never on production):

```bash
odoo-impact build-registry --odoo-bin /path/to/odoo-bin --python /path/to/odoo-venv/bin/python \
  -c /path/to/odoo.conf -d <local_db>
```

Smoke test: `odoo-impact find <a custom model name>` then `odoo-impact field <model.field>` on a
field the user knows. The build is recorded in the graph: `odoo-impact rebuild` redoes it.

## 4. Keep it out of git

The graph contains absolute paths. Add it to the local exclude, not to a shared `.gitignore`:

```bash
grep -qx '.odoo-impact/' .git/info/exclude 2>/dev/null || echo '.odoo-impact/' >> .git/info/exclude
```

## 5. Register the MCP server

Use the absolute graph path. Registering changes the agent's own configuration: tell the user the
exact command or snippet and apply it only after they approve. Pick the agent in use:

```bash
# Claude Code (private to this project)
claude mcp add odoo-impact -s local -- odoo-impact mcp -g "$PWD/.odoo-impact/graph.json"
claude mcp get odoo-impact          # expect: Status ✔ Connected
```

Other agents (Codex, Cursor, Claude Desktop...): give the user the snippet from section
"5. Connect your AI coding agent" of the repository README, with the absolute graph path filled in,
for them to add to their agent settings.

If `odoo-impact` is not on the agent's PATH, use the absolute path of the executable (`which odoo-impact`).
MCP servers load when a session starts: tell the user the `impact_*` tools appear in the next session.

## 6. Install the skills

```bash
git clone https://github.com/stephenprasetyachrismawan/odoo-impact-graph.git /tmp/odoo-impact-graph
/tmp/odoo-impact-graph/scripts/install-skills.sh            # into ~/.agents/skills (or ~/.claude/skills)
/tmp/odoo-impact-graph/scripts/install-skills.sh --target <project>/.agents/skills   # per project
```

If the machine has a skill review step (for example `skillspector scan <dir> --no-llm`), run it on
each installed folder. Agents load skills at session start: restart the agent.

## Done when

`odoo-impact field <custom model.field>` prints a tree and screens, `claude mcp get odoo-impact`
(or the agent's equivalent) shows connected, and the install script printed `installed` for the four
odoo-impact skills.
