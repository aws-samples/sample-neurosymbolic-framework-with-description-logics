"""Query program for an OWL ontology.

This program uses Opus 4.6 to UNDERSTAND the question (decompose it into
a structured query), then uses owlready2 to ANSWER it by traversing the
ontology graph. The LLM never sees the answer — it only parses the question.
"""
import json
import sys
from pathlib import Path

import boto3
import owlready2


# --- Step 1: Use LLM to decompose the question into a structured query ---

def decompose_question(question: str, bedrock_client) -> dict:
    """Use Opus 4.6 to parse a natural language question into a structured query.

    Returns a dict with:
        - target: the main entity/class being asked about (e.g. "heart")
        - relation: the relationship being queried (e.g. "has_part", "subclass_of",
          "function", "definition", "property_value")
        - filter: optional filter or constraint (e.g. "direct only")
        - keywords: list of additional search terms
    """
    system_prompt = (
        "You are a question parser for OWL ontology queries. Given a natural language "
        "question, decompose it into a structured JSON query that can drive an ontology "
        "traversal. Do NOT answer the question. Only parse it.\n\n"
        "Return a JSON object with these fields:\n"
        "- target: the main entity/concept being asked about (string)\n"
        "- relation: one of: has_part, part_of, subclass_of, superclass_of, "
        "function, definition, property_value, instances, related_to (string)\n"
        "- filter: any constraint or qualifier (string or null)\n"
        "- keywords: additional search terms to find the target in the ontology (list of strings)\n\n"
        "Return ONLY valid JSON, no other text."
    )

    response = bedrock_client.converse(
        modelId="us.anthropic.claude-opus-4-6-v1",
        messages=[
            {
                "role": "user",
                "content": [{"text": question}],
            }
        ],
        system=[{"text": system_prompt}],
    )

    response_text = response["output"]["message"]["content"][0]["text"].strip()

    # Parse JSON from response (handle markdown code blocks)
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        response_text = "\n".join(lines[1:-1])

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        # Fallback: extract keywords manually
        return {
            "target": question.split()[-1].rstrip("?"),
            "relation": "related_to",
            "filter": None,
            "keywords": question.lower().replace("?", "").split(),
        }


# --- Step 2: Use owlready2 to execute the structured query ---

def find_classes(onto, target: str, keywords: list[str]) -> list:
    """Find ontology classes matching the target and keywords."""
    results = []
    search_terms = [target.lower().replace(" ", "_")] + [k.lower() for k in keywords]

    for cls in onto.classes():
        name = (cls.name or "").lower().replace("_", " ")
        for term in search_terms:
            term_normalized = term.replace("_", " ")
            if term_normalized in name or name in term_normalized:
                results.append(cls)
                break

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for cls in results:
        if cls not in seen:
            seen.add(cls)
            unique.append(cls)
    return unique


def get_parts(cls, onto) -> list:
    """Find parts of a class using part-related object properties and restrictions."""
    parts = []
    part_props = []
    for prop in onto.object_properties():
        prop_name = (prop.name or "").lower()
        if any(term in prop_name for term in ["part", "component", "constituent", "member"]):
            part_props.append(prop)

    # Check class restrictions
    for ancestor in cls.is_a:
        if isinstance(ancestor, owlready2.class_construct.Restriction):
            if ancestor.property in part_props:
                if hasattr(ancestor, "value") and ancestor.value:
                    parts.append(ancestor.value)

    # Check subclasses for part restrictions
    for sub in cls.subclasses():
        for restriction in sub.is_a:
            if isinstance(restriction, owlready2.class_construct.Restriction):
                if restriction.property in part_props:
                    if hasattr(restriction, "value") and restriction.value:
                        parts.append(restriction.value)

    return parts


def get_subclasses(cls, depth: int = 1) -> list:
    """Get subclasses up to a given depth."""
    if depth <= 0:
        return []
    subs = list(cls.subclasses())
    if depth > 1:
        for sub in list(subs):
            subs.extend(get_subclasses(sub, depth - 1))
    return subs


def get_superclasses(cls) -> list:
    """Get direct named superclasses."""
    return [p for p in cls.is_a if isinstance(p, owlready2.entity.ThingClass)]


def get_annotations(cls) -> list[str]:
    """Get annotations (comments, labels) for a class."""
    annotations = []
    if hasattr(cls, "comment") and cls.comment:
        annotations.extend(str(c) for c in cls.comment)
    if hasattr(cls, "label") and cls.label:
        annotations.extend(str(l) for l in cls.label)
    return annotations


def get_instances(cls) -> list:
    """Get individuals that are instances of a class."""
    return list(cls.instances())


def format_name(entity) -> str:
    """Format an entity name for display."""
    name = entity.name if hasattr(entity, "name") else str(entity)
    return name.replace("_", " ")


def execute_query(onto, query: dict) -> str:
    """Execute a structured query against the ontology using owlready2."""
    target = query.get("target", "")
    relation = query.get("relation", "related_to")
    keywords = query.get("keywords", [])

    # Find the target class(es)
    classes = find_classes(onto, target, keywords)

    if not classes:
        return f"No classes found matching '{target}' in the ontology."

    primary = classes[0]

    if relation == "has_part":
        parts = get_parts(primary, onto)
        if parts:
            names = [format_name(p) for p in parts[:30]]
            return f"The parts of {format_name(primary)} include: {', '.join(names)}."
        # Fallback to subclasses
        subs = get_subclasses(primary, depth=1)
        if subs:
            names = [format_name(s) for s in subs[:20]]
            return f"{format_name(primary)} includes: {', '.join(names)}."
        return f"No parts found for {format_name(primary)}."

    elif relation == "part_of":
        parents = get_superclasses(primary)
        if parents:
            names = [format_name(p) for p in parents]
            return f"{format_name(primary)} is part of / subclass of: {', '.join(names)}."
        return f"No parent classes found for {format_name(primary)}."

    elif relation == "subclass_of":
        subs = get_subclasses(primary, depth=1)
        if subs:
            names = [format_name(s) for s in subs[:30]]
            return f"Subclasses of {format_name(primary)}: {', '.join(names)}."
        return f"No subclasses found for {format_name(primary)}."

    elif relation == "superclass_of":
        parents = get_superclasses(primary)
        if parents:
            names = [format_name(p) for p in parents]
            return f"{format_name(primary)} is a subclass of: {', '.join(names)}."
        return f"No superclasses found for {format_name(primary)}."

    elif relation == "function" or relation == "definition":
        annotations = get_annotations(primary)
        if annotations:
            return f"{format_name(primary)}: {'; '.join(annotations[:5])}"
        # Fallback: describe via subclasses
        subs = get_subclasses(primary, depth=1)
        if subs:
            names = [format_name(s) for s in subs[:10]]
            return f"{format_name(primary)} encompasses: {', '.join(names)}."
        return f"No definition or annotations found for {format_name(primary)}."

    elif relation == "instances":
        instances = get_instances(primary)
        if instances:
            names = [format_name(i) for i in instances[:20]]
            return f"Instances of {format_name(primary)}: {', '.join(names)}."
        return f"No instances found for {format_name(primary)}."

    elif relation == "property_value":
        # Look for data property values on the class or its instances
        info = []
        for prop in onto.data_properties():
            vals = getattr(primary, prop.name, None)
            if vals:
                info.append(f"{prop.name}: {vals}")
        if info:
            return f"{format_name(primary)} properties: {'; '.join(info[:10])}"
        return f"No property values found for {format_name(primary)}."

    else:  # related_to or unknown
        # Provide general info
        info_parts = [format_name(primary)]
        parents = get_superclasses(primary)
        if parents:
            info_parts.append(f"is a {', '.join(format_name(p) for p in parents[:3])}")
        subs = get_subclasses(primary, depth=1)
        if subs:
            info_parts.append(f"includes {', '.join(format_name(s) for s in subs[:10])}")
        annotations = get_annotations(primary)
        if annotations:
            info_parts.append(annotations[0])
        return ". ".join(info_parts) + "."


# --- Main entry point ---

def main(onto_file: Path, question: str) -> None:
    """Answer a question about the ontology.

    Uses Opus 4.6 to understand/decompose the question, then owlready2 to answer it.

    Args:
        onto_file: Path to the OWL XML file.
        question: Natural language question to answer.
    """
    # Load ontology with SQLite caching for faster repeated loads
    cache_file = onto_file.with_suffix(".sqlite3")
    owlready2.default_world.set_backend(filename=str(cache_file))
    onto = owlready2.get_ontology(f"file://{onto_file}").load()

    # Use LLM to decompose the question into a structured query
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    query = decompose_question(question, client)

    # Execute the structured query against the ontology
    answer = execute_query(onto, query)
    print(f"Answer: {answer}")


if __name__ == "__main__":
    onto_file = Path(sys.argv[1])
    question = sys.argv[2]
    main(onto_file, question)
