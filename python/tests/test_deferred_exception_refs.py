"""No deferred callback may reference an exception variable.

Python deletes the ``as e`` name when an except block ends. A callback created
inside that block and run later — ``after(0, lambda: ... e ...)``, a thread
target, a scheduled retry — therefore raises::

    NameError: cannot access free variable 'e' where it is not associated
    with a value in enclosing scope

The nasty part is *where* this lands. It only fires on the error path, so the
handler meant to explain a failure becomes a second, unrelated failure — the
user gets a NameError instead of "the server is down". It is invisible to a
syntax check, invisible in testing that never provokes an error, and it survived
in this file until a user hit it by clicking Evaluate.

Two live instances existed when this was written: the Discuss error handler and
the evaluator-prompt download fallback.

The fix is always the same: resolve the message while the variable still
exists, then bind it as a default argument::

    except Exception as e:
        msg = _error_line(e)
        self.after(0, lambda m=msg: show(m))
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import agentpub

SDK_ROOT = Path(agentpub.__file__).parent
MODULES = sorted(SDK_ROOT.glob("*.py"))


def _offenders(path: Path) -> list[tuple[int, str, str]]:
    """Deferred callables inside an except block that use its exception name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str, str]] = []

    class Visitor(ast.NodeVisitor):
        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            name = node.name
            if name:
                for sub in ast.walk(node):
                    if not isinstance(sub, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    # A parameter of the same name rebinds it safely — that is
                    # the `lambda m=msg:` fix, and must not be flagged.
                    if isinstance(sub, ast.Lambda):
                        bound = {a.arg for a in sub.args.args} | {a.arg for a in sub.args.kwonlyargs}
                    else:
                        bound = {a.arg for a in sub.args.args}
                    if name in bound:
                        continue
                    for inner in ast.walk(sub):
                        if isinstance(inner, ast.Name) and inner.id == name:
                            found.append((inner.lineno, name, type(sub).__name__))
                            break
            self.generic_visit(node)

    Visitor().visit(tree)
    return sorted(set(found))


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_no_deferred_callback_uses_a_dead_exception_variable(module: Path):
    offenders = _offenders(module)
    assert not offenders, "\n".join(
        [f"{module.name}: deferred callables reference a deleted exception name:"]
        + [
            f"  line {line}: {kind} uses {name!r} after its except block ends"
            for line, name, kind in offenders
        ]
        + [
            "",
            "Python deletes the `as` name at the end of the except block, so this",
            "raises NameError when the callback actually runs. Capture the text",
            "first and bind it:  msg = _error_line(e); after(0, lambda m=msg: ...)",
        ]
    )


def test_the_detector_actually_detects(tmp_path):
    """A guard that cannot fail is not a guard.

    The two real instances are fixed, so without this the test above would pass
    on an empty file forever and prove nothing.
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f(win):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        win.after(0, lambda: print(e))\n",
        encoding="utf-8",
    )
    assert _offenders(bad), "detector failed to spot a known-bad pattern"

    good = tmp_path / "good.py"
    good.write_text(
        "def f(win):\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        msg = str(e)\n"
        "        win.after(0, lambda m=msg: print(m))\n",
        encoding="utf-8",
    )
    assert not _offenders(good), "detector flagged the correct pattern"
