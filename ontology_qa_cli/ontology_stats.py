"""Generate descriptive statistics for ontologies in the config."""
import sqlite3
import sys
from pathlib import Path

import owlready2

from ontology_qa_cli.config import load_config


def _stats_from_custom_db(db_path: Path, file_size: int, line_count: int) -> dict:
    """Extract stats from a custom SQLite DB (e.g. built by build_db.py for SNOMED)."""
    conn = sqlite3.connect(str(db_path))
    tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    # Get concept count from labels table
    concepts = 0
    if "labels" in tables:
        concepts = conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]

    # Count relations — handle different schemas
    total_relations = 0
    for table in ["subclass", "subclassof", "roles", "equiv_roles", "equiv_parents"]:
        if table in tables:
            total_relations += conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # Count properties if available
    properties = None
    if "property_labels" in tables:
        properties = conn.execute("SELECT COUNT(*) FROM property_labels").fetchone()[0]

    conn.close()

    return {
        "file_size_bytes": file_size,
        "file_size_mb": file_size / (1024 * 1024),
        "line_count": line_count,
        "classes": concepts,
        "object_properties": properties,
        "data_properties": None,
        "annotation_properties": None,
        "individuals": None,
        "axioms_restrictions": total_relations,
    }


def get_ontology_stats(owl_file: Path) -> dict:
    """Load an ontology and compute descriptive statistics."""
    owl_file = owl_file.expanduser()
    if not owl_file.exists():
        return {"error": f"File not found: {owl_file}"}

    # File stats
    file_size = owl_file.stat().st_size
    with open(owl_file, "r", errors="replace") as f:
        line_count = sum(1 for _ in f)

    # Load via SQLite cache for speed, fall back to direct load
    cache_file = owl_file.with_suffix(".sqlite3")
    world = owlready2.World()
    try:
        if cache_file.exists() and cache_file.stat().st_size > 0:
            world.set_backend(filename=str(cache_file))
        onto = world.get_ontology(f"file://{owl_file}").load()
    except Exception as e:
        # Retry without cache
        try:
            world = owlready2.World()
            onto = world.get_ontology(f"file://{owl_file}").load()
        except Exception as e2:
            # Can't load via owlready2 — try custom SQLite DB (e.g. SNOMED)
            custom_db = owl_file.with_suffix(".db")
            if not custom_db.exists():
                # Also check for snomed.db in same directory
                custom_db = owl_file.parent / "snomed.db"
            if custom_db.exists():
                return _stats_from_custom_db(custom_db, file_size, line_count)
            return {
                "file_size_bytes": file_size,
                "file_size_mb": file_size / (1024 * 1024),
                "line_count": line_count,
                "classes": None,
                "object_properties": None,
                "data_properties": None,
                "annotation_properties": None,
                "individuals": None,
                "axioms_restrictions": None,
                "load_error": str(e2),
            }

    # Count entities
    classes = list(onto.classes())
    object_properties = list(onto.object_properties())
    data_properties = list(onto.data_properties())
    individuals = list(onto.individuals())
    annotation_properties = list(onto.annotation_properties())

    # Count axioms (restrictions on classes)
    axiom_count = 0
    for cls in classes:
        for parent in cls.is_a:
            if isinstance(parent, owlready2.class_construct.Restriction):
                axiom_count += 1

    world.close()

    return {
        "file_size_bytes": file_size,
        "file_size_mb": file_size / (1024 * 1024),
        "line_count": line_count,
        "classes": len(classes),
        "object_properties": len(object_properties),
        "data_properties": len(data_properties),
        "annotation_properties": len(annotation_properties),
        "individuals": len(individuals),
        "axioms_restrictions": axiom_count,
    }


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.0f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


def generate_stats_table(config_path: Path = Path("ontology_config.yaml")) -> str:
    """Generate a markdown table with stats for all ontologies in the config."""
    config = load_config(config_path)

    lines = []
    lines.append("| Ontology | File Size | Lines | Classes | Object Properties | Data Properties | Individuals | Axioms (Restrictions) |")
    lines.append("|----------|-----------|-------|---------|-------------------|-----------------|-------------|----------------------|")

    for entry in config.ontologies:
        print(f"Computing stats for {entry.name}...", file=sys.stderr)
        stats = get_ontology_stats(entry.owl_file)
        if "error" in stats:
            lines.append(f"| {entry.name} | {stats['error']} | | | | | | |")
        else:
            def fmt(val, comma=True):
                if val is None:
                    return "—"
                return f"{val:,}" if comma else str(val)

            lines.append(
                f"| {entry.name} "
                f"| {format_size(stats['file_size_bytes'])} "
                f"| {stats['line_count']:,} "
                f"| {fmt(stats['classes'])} "
                f"| {fmt(stats['object_properties'])} "
                f"| {fmt(stats['data_properties'])} "
                f"| {fmt(stats['individuals'])} "
                f"| {fmt(stats['axioms_restrictions'])} |"
            )

    return "\n".join(lines)


if __name__ == "__main__":
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ontology_config.yaml")
    print(generate_stats_table(config_path))
