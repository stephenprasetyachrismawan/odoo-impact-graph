---
name: odoo-impact-web
description: Opens the odoo-impact web explorer - the interactive page that shows an Odoo impact graph as five lanes (menu/action, view/button, model, logic, field) with highlighted paths - in the browser, served from localhost, and explains how to read it. Use when the user wants to see, show, open or browse the impact graph visually ("lihat web impact graph", "buka explorer", "tampilkan graf dampak"), or wants a link or screenshot of the impact of a field, method, button or menu.
---

# Odoo impact web explorer

Needs a built graph (`<project>/.odoo-impact/graph.json`); otherwise run `odoo-impact-setup`.

## Open it

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

`#<mode>|<node id>` opens a question directly, mode = `impact`, `who` or `from`:

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
