"""Write the self-contained HTML explorer (the graph is embedded, opens from file://)."""
import os

TEMPLATE = os.path.join(os.path.dirname(__file__), 'explorer.html')


def write_html(graph_path, out_path):
    with open(graph_path, encoding='utf-8') as fh:
        graph = fh.read().replace('</', '<\\/')
    with open(TEMPLATE, encoding='utf-8') as fh:
        page = fh.read().replace('/*__GRAPH__*/null', graph, 1)
    with open(out_path, 'w', encoding='utf-8') as fh:
        fh.write(page)
    return out_path
