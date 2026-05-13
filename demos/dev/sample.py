"""Sample target file for the dev demo.

This file intentionally lacks docstrings so the demo can show an agent
adding them via delegate_to_agent.
"""


def add(a, b):
    """Return the sum of a and b."""
    return a + b


def subtract(a, b):
    """Return the difference of a and b."""
    return a - b


def multiply(a, b):
    """Return the product of a and b."""
    return a * b


def divide(a, b):
    """Return the quotient of a divided by b."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b


class Calculator:
    def __init__(self):
        """Initialize a Calculator with an empty history."""
        self.history = []

    def compute(self, op, a, b):
        """Apply the given operation to a and b and record in history."""
        ops = {"add": add, "sub": subtract, "mul": multiply, "div": divide}
        if op not in ops:
            raise ValueError(f"unknown op: {op!r}")
        result = ops[op](a, b)
        self.history.append((op, a, b, result))
        return result

    def last(self):
        """Return the last computation result or None."""
        return self.history[-1] if self.history else None
