"""Exit 1 if a pytest JUnit report has skipped tests or no tests: in CI every test, including the PostgreSQL
integration tests, must run."""

import sys
import xml.etree.ElementTree as ET


def main(path: str) -> int:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    print(f"{tests} tests, {skipped} skipped")
    return 1 if skipped or not tests else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
