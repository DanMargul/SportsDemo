import ast
import pathlib

SOURCE_DIRECTORY = (pathlib.Path(__file__).resolve().parent.parent
                    / "src" / "sports_markets")


def source_files():
    return [path for path in sorted(SOURCE_DIRECTORY.glob("*.py"))
            if not path.stem.startswith(("__", "first_"))]


def test_every_function_is_fully_annotated():
    unannotated = []
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            arguments = [argument for argument in node.args.args
                         if argument.arg not in ("self", "cls")]
            arguments += node.args.kwonlyargs
            missing = [argument.arg for argument in arguments
                       if argument.annotation is None]
            if node.returns is None:
                missing.append("return")
            if missing:
                unannotated.append(f"{path.name}:{node.lineno} {node.name} "
                                   f"({', '.join(missing)})")
    assert not unannotated, ("functions missing type hints:\n  "
                            + "\n  ".join(unannotated))
    print("PASS every function carries full type hints")


def test_declared_types_agree_with_the_code():
    import shutil
    import subprocess
    if shutil.which("mypy") is None:
        print("SKIP mypy is not installed")
        return
    result = subprocess.run(
        ["mypy", str(SOURCE_DIRECTORY), "--ignore-missing-imports",
         "--no-error-summary"], capture_output=True, text=True)
    contradictions = [line for line in result.stdout.splitlines()
                      if any(code in line for code in
                             ("[return-value]", "[arg-type]", "[call-arg]",
                              "[func-returns-value]"))]
    assert not contradictions, ("type hints contradict the code:\n  "
                                + "\n  ".join(contradictions))
    print("PASS declared types agree with what the code does")


def test_no_comments_in_source():
    commented = []
    for path in source_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") and not stripped.startswith("#!"):
                commented.append(f"{path.name}:{number}")
    assert not commented, ("comments found; names and structure should carry "
                           "the meaning:\n  " + "\n  ".join(commented))
    print("PASS no explanatory comments in source")


def test_f_strings_always_interpolate():
    decorative = []
    for path in source_files():
        tree = ast.parse(path.read_text())
        format_specs = {id(node.format_spec) for node in ast.walk(tree)
                        if isinstance(node, ast.FormattedValue)
                        and node.format_spec is not None}
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr) or id(node) in format_specs:
                continue
            if not any(isinstance(part, ast.FormattedValue)
                       for part in node.values):
                decorative.append(f"{path.name}:{node.lineno}")
    assert not decorative, ("f-strings without placeholders should be plain "
                            "strings:\n  " + "\n  ".join(decorative))
    print("PASS every f-string interpolates something")


def test_callable_keywords_match_their_definitions():
    definitions, definition_counts, calls = {}, {}, []
    for path in source_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = {argument.arg for argument in node.args.args}
                names |= {argument.arg for argument in node.args.kwonlyargs}
                definitions.setdefault(node.name, set()).update(names)
                definition_counts[node.name] = (
                    definition_counts.get(node.name, 0) + 1)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.keywords:
                continue
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            else:
                continue
            calls.append((path.name, node.lineno, name,
                          [keyword.arg for keyword in node.keywords
                           if keyword.arg]))

    mismatches = []
    for file_name, line, name, keywords in calls:
        accepted = definitions.get(name)
        if accepted is None or definition_counts.get(name, 0) != 1:
            continue
        unknown = [keyword for keyword in keywords if keyword not in accepted]
        if unknown:
            mismatches.append(f"{file_name}:{line} {name}({', '.join(unknown)}=)")
    assert not mismatches, ("calls pass keywords the definition does not "
                            "accept:\n  " + "\n  ".join(mismatches))
    print("PASS keyword arguments match the definitions they call")


TESTS = [
    test_every_function_is_fully_annotated,
    test_declared_types_agree_with_the_code,
    test_no_comments_in_source,
    test_f_strings_always_interpolate,
    test_callable_keywords_match_their_definitions,
]


def main():
    for test in TESTS:
        test()
    print(f"\n{len(TESTS)} tests passed in test_style.py")


if __name__ == "__main__":
    main()
