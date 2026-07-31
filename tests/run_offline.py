import importlib
import pathlib
import sys
import traceback

TESTS_DIRECTORY = pathlib.Path(__file__).resolve().parent


def test_modules():
    sys.path.insert(0, str(TESTS_DIRECTORY))
    for path in sorted(TESTS_DIRECTORY.glob("test_*.py")):
        yield importlib.import_module(path.stem)


def unregistered_tests(module):
    registered = {test.__name__ for test in getattr(module, "TESTS", ())}
    defined = {name for name in vars(module)
               if name.startswith("test_") and callable(getattr(module, name))}
    return sorted(defined - registered)


def main():
    modules = list(test_modules())
    omissions = [(module.__name__, name) for module in modules
                 for name in unregistered_tests(module)]
    if omissions:
        print("tests defined but missing from their module's TESTS list:")
        for module_name, name in omissions:
            print(f"  {module_name}.{name}")
        raise SystemExit(1)

    failures, total = [], 0
    for module in modules:
        for test in module.TESTS:
            total += 1
            try:
                test()
            except Exception:
                failures.append(f"{module.__name__}.{test.__name__}")
                print(f"\nFAIL {module.__name__}.{test.__name__}")
                traceback.print_exc()

    if failures:
        print(f"\n{len(failures)} of {total} tests failed")
        raise SystemExit(1)
    print(f"\nall offline tests passed ({total} tests in "
          f"{len(modules)} modules)")


if __name__ == "__main__":
    main()
