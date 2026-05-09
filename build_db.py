"""Build a SQLite database from the SNOMED OWL functional syntax file."""
import re
import sqlite3
import sys

def build_db(owl_path, db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS labels (id TEXT PRIMARY KEY, label TEXT, pref_label TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS subclass (child TEXT, parent TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS roles (subject TEXT, property TEXT, filler TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS equiv_roles (subject TEXT, property TEXT, filler TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS equiv_parents (child TEXT, parent TEXT)")
    
    # Regex patterns
    label_pat = re.compile(r'AnnotationAssertion\(rdfs:label :(\d+) "(.*?)"@')
    pref_pat = re.compile(r'AnnotationAssertion\(skos:prefLabel :(\d+) "(.*?)"@')
    
    # Simple subclass: SubClassOf(:X :Y)
    simple_sub_pat = re.compile(r'^SubClassOf\(:(\d+) :(\d+)\)$')
    
    # SubClassOf with intersection - extract parents and roles
    sub_inter_pat = re.compile(r'^SubClassOf\(:(\d+) ObjectIntersectionOf\((.*)\)\)$')
    
    # EquivalentClasses
    equiv_pat = re.compile(r'^EquivalentClasses\(:(\d+) ObjectIntersectionOf\((.*)\)\)$')
    
    # ObjectSomeValuesFrom(:prop :filler) - simple role
    role_pat = re.compile(r'ObjectSomeValuesFrom\(:(\d+) :(\d+)\)')
    
    # Parent class refs in intersection
    parent_in_inter = re.compile(r':(\d+)')
    
    labels = {}
    pref_labels = {}
    subclass_rows = []
    role_rows = []
    equiv_parent_rows = []
    equiv_role_rows = []
    
    batch_size = 100000
    
    with open(owl_path, 'r') as f:
        for line in f:
            line = line.strip()
            
            # Labels
            m = label_pat.match(line)
            if m:
                labels[m.group(1)] = m.group(2)
                continue
            
            m = pref_pat.match(line)
            if m:
                pref_labels[m.group(1)] = m.group(2)
                continue
            
            # Simple SubClassOf
            m = simple_sub_pat.match(line)
            if m:
                subclass_rows.append((m.group(1), m.group(2)))
                if len(subclass_rows) >= batch_size:
                    conn.executemany("INSERT INTO subclass VALUES (?,?)", subclass_rows)
                    subclass_rows = []
                continue
            
            # SubClassOf with intersection
            m = sub_inter_pat.match(line)
            if m:
                child = m.group(1)
                content = m.group(2)
                # Extract simple roles
                for role_m in role_pat.finditer(content):
                    role_rows.append((child, role_m.group(1), role_m.group(2)))
                # Extract parent classes (bare :ID references at top level of intersection)
                # Remove ObjectSomeValuesFrom(...) parts first
                cleaned = re.sub(r'ObjectSomeValuesFrom\([^)]*\)', '', content)
                # Also remove nested ObjectSomeValuesFrom
                cleaned = re.sub(r'ObjectSomeValuesFrom\([^)]*\([^)]*\)[^)]*\)', '', cleaned)
                for pm in re.finditer(r':(\d+)', cleaned):
                    subclass_rows.append((child, pm.group(1)))
                
                if len(subclass_rows) >= batch_size:
                    conn.executemany("INSERT INTO subclass VALUES (?,?)", subclass_rows)
                    subclass_rows = []
                if len(role_rows) >= batch_size:
                    conn.executemany("INSERT INTO roles VALUES (?,?,?)", role_rows)
                    role_rows = []
                continue
            
            # EquivalentClasses
            m = equiv_pat.match(line)
            if m:
                child = m.group(1)
                content = m.group(2)
                # Extract roles (simple ones)
                for role_m in role_pat.finditer(content):
                    equiv_role_rows.append((child, role_m.group(1), role_m.group(2)))
                # Extract parent classes
                cleaned = re.sub(r'ObjectSomeValuesFrom\([^)]*\)', '', content)
                cleaned = re.sub(r'ObjectSomeValuesFrom\([^)]*\([^)]*\)[^)]*\)', '', cleaned)
                for pm in re.finditer(r':(\d+)', cleaned):
                    equiv_parent_rows.append((child, pm.group(1)))
                
                if len(equiv_parent_rows) >= batch_size:
                    conn.executemany("INSERT INTO equiv_parents VALUES (?,?)", equiv_parent_rows)
                    equiv_parent_rows = []
                if len(equiv_role_rows) >= batch_size:
                    conn.executemany("INSERT INTO equiv_roles VALUES (?,?,?)", equiv_role_rows)
                    equiv_role_rows = []
                continue
    
    # Flush remaining
    if subclass_rows:
        conn.executemany("INSERT INTO subclass VALUES (?,?)", subclass_rows)
    if role_rows:
        conn.executemany("INSERT INTO roles VALUES (?,?,?)", role_rows)
    if equiv_parent_rows:
        conn.executemany("INSERT INTO equiv_parents VALUES (?,?)", equiv_parent_rows)
    if equiv_role_rows:
        conn.executemany("INSERT INTO equiv_roles VALUES (?,?,?)", equiv_role_rows)
    
    # Insert labels
    label_rows = [(k, v, pref_labels.get(k, '')) for k, v in labels.items()]
    conn.executemany("INSERT OR REPLACE INTO labels VALUES (?,?,?)", label_rows)
    
    # Create indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subclass_child ON subclass(child)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subclass_parent ON subclass(parent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roles_subject ON roles(subject)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roles_property ON roles(property)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roles_filler ON roles(filler)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roles_prop_filler ON roles(property, filler)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_equiv_roles_subject ON equiv_roles(subject)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_equiv_roles_prop_filler ON equiv_roles(property, filler)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_equiv_parents_child ON equiv_parents(child)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_equiv_parents_parent ON equiv_parents(parent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_labels_label ON labels(label COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_labels_pref ON labels(pref_label COLLATE NOCASE)")
    
    conn.commit()
    conn.close()
    print("Done!")

if __name__ == "__main__":
    build_db(sys.argv[1], sys.argv[2])
