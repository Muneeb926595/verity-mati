"""What kind of code a file holds, from its path alone: test code, documentation/example code and static-analysis
model files are "secondary" — real code a project ships, but not the program itself. Used to prefer the program's own
definition when a name is defined twice, and to rank the program's code first in answers."""
from __future__ import annotations

import re

TEST_PATH_RE = re.compile(r"(^|/)(tests?|unittests?|unit_tests?|testing|[\w.]*\.Tests?|(?-i:[\w.]*Tests|Test[A-Z][\w.-]*))/|(^|/)test_[^/]*$|"
                          r"_tests?\.(c|cc|cpp|cs)$|Tests?\.cs$|(^|/)(unit_?)?tests?\.(c|cc|cpp|h)$", re.I)
DOC_PATH_RE = re.compile(r"(^|/)(docs?|documentation|examples?|samples?|demos?)/", re.I)
MODEL_PATH_RE = re.compile(r"(^|/)[^/]*(coverity|klocwork|polyspace|cppcheck|frama)[^/]*(/|$)", re.I)
# alternative ports and third-party trees shipped alongside the program (SOEM contrib/oshw/erika): when a name is defined
# both there and in the program's own tree, the program's definition is the one to describe
VENDOR_PATH_RE = re.compile(r"(^|/)(contrib|third[_-]?party|3rd[_-]?party)/", re.I)
TEST_WORDS_RE = re.compile(r"\b(tests?|tested|testing|unit[- ]?tests?)\b", re.I)


def is_test_path(path: str | None) -> bool:
    return bool(path) and bool(TEST_PATH_RE.search(path))


def is_secondary_path(path: str | None) -> bool:
    """Tests, docs/examples, analysis-tool model files and contrib / third-party trees."""
    return bool(path) and bool(TEST_PATH_RE.search(path) or DOC_PATH_RE.search(path) or MODEL_PATH_RE.search(path) or
                               VENDOR_PATH_RE.search(path))
