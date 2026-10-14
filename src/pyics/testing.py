"""
Helpers for the sources of ``GeneralTest``s (see ``pyics.tests.general``), e.g.::

    from pyics.testing import capture_stdout, provide_input

    def test_greets(solution):
        with capture_stdout() as out, provide_input(["Bob"]):
            solution.greet()
        assert out.getvalue() == "Name: Bob\\nHello, Bob!\\n"

- ``capture_stdout()``: captures what the calling thread writes to stdout, in a
  ``StringIO``.
- ``provide_input(inputs)``: answers the calling thread's ``input()`` calls with
  ``inputs``, in order, echoing them like a console.
"""

from pyics.tests.capture import capture_stdout, provide_input

__all__ = ["capture_stdout", "provide_input"]
