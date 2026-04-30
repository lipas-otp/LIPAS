import ast
from pathlib import Path


# ========= Docstring extract =========

def extract_docstrings(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    module_doc = ast.get_docstring(tree)

    classes = []
    functions = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "doc": ast.get_docstring(node),
                "methods": extract_methods(node)
            })

        elif isinstance(node, ast.FunctionDef):
            functions.append({
                "name": node.name,
                "doc": ast.get_docstring(node)
            })

    return {
        "module_doc": module_doc,
        "classes": classes,
        "functions": functions
    }


def extract_methods(class_node):
    methods = []
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef):
            methods.append({
                "name": node.name,
                "doc": ast.get_docstring(node)
            })
    return methods


# ========= dir tree =========

def build_tree(root):
    lines = []

    def _walk(path, prefix=""):
        items = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))

        for i, item in enumerate(items):
            connector = "└── " if i == len(items) - 1 else "├── "
            lines.append(prefix + connector + item.name)

            if item.is_dir():
                extension = "    " if i == len(items) - 1 else "│   "
                _walk(item, prefix + extension)

    _walk(Path(root))
    return "\n".join(lines)


# ========= Markdown generating =========

def generate_markdown(project_root):
    root = Path(project_root)
    md = []

    # project title
    md.append(f"# Project Documentation\n")
    md.append(f"Root: `{root.resolve()}`\n")

    # dir tree
    md.append("## 📁 Project Structure\n")
    md.append("```\n")
    md.append(build_tree(root))
    md.append("\n```\n")

    # all python scripts
    for py_file in root.rglob("*.py"):
        rel_path = py_file.relative_to(root)

        md.append(f"\n---\n")
        md.append(f"## 📄 `{rel_path}`\n")

        data = extract_docstrings(py_file)

        # module doc
        if data["module_doc"]:
            md.append(f"\n**Module Doc:**\n")
            md.append(f"> {data['module_doc']}\n")

        # classes
        if data["classes"]:
            md.append(f"\n### 🧱 Classes\n")
            for cls in data["classes"]:
                md.append(f"\n#### `{cls['name']}`\n")

                if cls["doc"]:
                    md.append(f"> {cls['doc']}\n")

                if cls["methods"]:
                    md.append(f"\n**Methods:**\n")
                    for m in cls["methods"]:
                        md.append(f"- `{m['name']}`")
                        if m["doc"]:
                            md.append(f"  - {m['doc']}")

        # functions
        if data["functions"]:
            md.append(f"\n### ⚙️ Functions\n")
            for fn in data["functions"]:
                md.append(f"- `{fn['name']}`")
                if fn["doc"]:
                    md.append(f"  - {fn['doc']}")

    return "\n".join(md)


# ========= main =========

if __name__ == "__main__":
    project_path = "../lipas"
    output_file = "./lipas_docstr.md"

    md = generate_markdown(project_path)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"✅ Documentation generated: {output_file}")
