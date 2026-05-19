"""Vizualizace BOM stromu a souhrnu primárních surovin pomocí Rich."""
from rich.console import Console
from rich.tree import Tree
from rich.table import Table
from rich.text import Text
from rich import box
from app.bom.resolver import BOMNode

console = Console()

ACTIVITY_COLOR = {
    "manufacturing": "cyan",
    "reaction": "magenta",
    "raw": "green",
}


def _node_label(node: BOMNode) -> Text:
    color = ACTIVITY_COLOR.get(node.activity, "white")
    label = Text()
    label.append(f"{node.name}", style=f"bold {color}" if not node.is_leaf else "green")
    label.append(f"  ×{node.quantity:,}", style="white")
    if not node.is_leaf:
        label.append(f"  ({node.runs} run{'s' if node.runs != 1 else ''})", style="dim")
    return label


def build_rich_tree(node: BOMNode, parent=None) -> Tree:
    label = _node_label(node)
    if parent is None:
        tree = Tree(label)
        current = tree
    else:
        current = parent.add(label)

    for child in node.children:
        build_rich_tree(child, current)

    return tree if parent is None else current


def print_bom_tree(root: BOMNode):
    console.print()
    console.print(f"[bold]Výrobní strom:[/] [cyan]{root.name}[/] ×{root.quantity:,}\n")
    tree = build_rich_tree(root)
    console.print(tree)


def print_primary_materials(root: BOMNode):
    leaves = root.aggregate_leaves()
    if not leaves:
        return

    table = Table(
        title=f"Primární suroviny pro výrobu: {root.name} ×{root.quantity:,}",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Surovina", style="green", min_width=30)
    table.add_column("Množství", justify="right", style="bold white")
    table.add_column("Type ID", style="dim", justify="right")

    # Seřadit abecedně
    for type_id, (name, qty) in sorted(leaves.items(), key=lambda x: x[1][0]):
        table.add_row(name, f"{qty:,}", str(type_id))

    console.print()
    console.print(table)
    console.print(f"\n[dim]Celkem různých primárních surovin: {len(leaves)}[/]")


def print_bom_stats(root: BOMNode):
    """Statistiky o hloubce a počtu uzlů stromu."""
    total_nodes = 0
    max_depth = 0
    manufactured = 0
    reactions = 0

    def walk(node: BOMNode, depth: int):
        nonlocal total_nodes, max_depth, manufactured, reactions
        total_nodes += 1
        max_depth = max(max_depth, depth)
        if node.activity == "manufacturing":
            manufactured += 1
        elif node.activity == "reaction":
            reactions += 1
        for child in node.children:
            walk(child, depth + 1)

    walk(root, 0)

    console.print(
        f"[dim]Statistiky stromu: {total_nodes} uzlů, "
        f"max hloubka {max_depth}, "
        f"{manufactured} manufacturing, "
        f"{reactions} reaction kroků[/]"
    )
