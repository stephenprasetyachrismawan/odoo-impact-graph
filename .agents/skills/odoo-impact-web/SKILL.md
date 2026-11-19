---
name: odoo-impact-web
description: Opens the odoo-impact web service - the localhost dashboard (http://127.0.0.1:8770/) that lists an Odoo project's branches with Generate (built from git, no checkout), Diagram and Changes, and the interactive diagram that draws the code as a combined UML class / use case / activity diagram (models and relations, security groups and menus/buttons, state workflows) with impact highlighting - and explains how to use them. Use when the user wants to see, show, open, browse or explore the code visually ("lihat web impact graph", "buka diagram", "tampilkan class diagram / use case / activity diagram", "graf dampak"), or wants a picture of a model, workflow or flow for a report.
---

# Odoo impact web service and explorer

## Dashboard (service)

```bash
odoo-impact service status       # running? (systemd --user unit `odoo-impact`)
odoo-impact service install      # install + start, or: odoo-impact serve --open
```

Give the user **http://127.0.0.1:8770/**. Per project:

- **Working copy**: the files on disk (uncommitted edits included).
- **Branches** (local; tick *include remote* for `origin/…`; filter box): **Generate** builds the branch's commit
  straight from git (`git archive`, no checkout), state *no graph* / *up to date* / *outdated*.
- **Diagram**: the interactive diagram of that build. **Changes**: what the branch touches vs another ref
  (default `origin/preprod`) and the screens to retest.

A project missing from the list → **Add project** (repository + odoo.conf, or core folders), or
`odoo-impact project add <repo> --conf <odoo.conf>`; see `odoo-impact-setup`.

## Interactive diagram

URL: `/diagram?project=<id>&build=<wc|commit sha>[&diagram=<saved slug>]` (the dashboard links it).

- Left panel, no search needed: **Modules** (custom first; models tagged `ext`, `wizard`, `flow`), **Menus**
  (real menu tree), **Actors** (security groups as Odoo names them, plus System (cron)). Click = put on canvas.
- Select a node; the right panel lists what it connects to with **add**: relations, parents, buttons (use cases
  with their groups), menus, access rights, workflows; for menus / buttons the actors and *what it can change*;
  for actors their menus, buttons and model access.
- Lenses on the same nodes: **Class** (UML class boxes: `/derived`, `[1]` required, custom in bold, `+public` /
  `-private` methods; associations with multiplicity, generalization for `_inherit`, composition for
  `_inherits`), **Use case** (actors → menus / buttons → model), **Activity** (state chart from the Selection field:
  buttons that write the state, from-states from `invisible` / `states` / `attrs`, groups; code / cron transitions
  behind a checkbox).
- Click a field in a class box = impact of changing it (highlighted rows, orange dashed arrows between models on
  the canvas, buttons to add affected models / menus, full text report). Click a method = what it can change.
- **Arrange**, **Fit**, drag / pan / wheel zoom, **Save** (named, stored in the service, reopen on any branch),
  **Open…**, **SVG** download.

To build a diagram for the user: open it, put the models / menus / actors they named on the canvas, pick the lens,
Arrange, Save under a clear name, and give them the URL with `&diagram=<slug>`.

## Single graph file (no service)

Needs a built graph (`<project>/.odoo-impact/graph.json`); otherwise run `odoo-impact-setup`.

```bash
odoo-impact web                        # http://127.0.0.1:8765/ and opens the browser
odoo-impact web --port 0 --no-open     # any free port, only print the URL (headless / remote shell)
odoo-impact web -g /abs/project/.odoo-impact/graph.json
```

It rewrites the page when the graph is newer, then serves only that page on localhost until Ctrl+C.
Run it in the background when the agent must keep working, and give the user the printed URL.

No server needed: `graph.html` next to `graph.json` opens straight from disk (`odoo-impact html`
regenerates it). In WSL the browser opens through `wslview` when installed; otherwise paste the URL in
the Windows browser (localhost is shared).

Remote machine: keep the default host `127.0.0.1` and tunnel (`ssh -L 8765:127.0.0.1:8765 host`);
do not bind `0.0.0.0`, the page lists the whole code structure.

## Deep link

`#<mode>|<node id>` opens a question directly in the explorer (service `/explore/<id>` or `odoo-impact web`),
mode = `impact`, `who` or `from`:

```
http://127.0.0.1:8765/#impact|sale.order.amount_total
http://127.0.0.1:8765/#who|sale.order.line.price_unit
http://127.0.0.1:8765/#from|menu:sale.menu_sale_quotations
```

Ids come from `odoo-impact find` / `impact_find` (URL-encode spaces).

## Read it

- Search box: field, method, view, button, menu, by id or label. A menu / action / button starts in
  **From here ↓**, a field or method keeps the current direction.
- **If it changes →** fields that recompute + logic that runs + screens showing the result.
  **← Who changes it** methods, buttons, menus, crons that write it. **From here ↓** every field an
  entry point can change.
- Lanes from top to bottom: L4 menu/action/cron, L3 view/button/report, L2 model/wizard, L1 logic,
  L0 field. "core" marks a method of a non-custom module.
- Hover a card: its path back to the start lights up. Click: side panel with files (`file:line`), all
  incoming / outgoing edges and the exact path. Double-click: start from that card.
- Depth (2-8) and "core logic" change how far it follows. Orange `~` edges come from method bodies
  (heuristic), check their `file:line`.

## Screenshot for a report

With Playwright / Chromium available, open the deep link, wait ~1 s for the edges to draw, hover the
card to highlight, capture 1300×800. Without a browser tool, give the deep link and the CLI text
(`odoo-impact field <id>`) instead.
