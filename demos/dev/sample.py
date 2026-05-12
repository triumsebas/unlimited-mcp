"""Sample target file for the dev demo.

This file intentionally lacks docstrings so the demo can show an agent
adding them via delegate_to_agent.
"""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b


class Calculator:
    def __init__(self):
        self.history = []

    def compute(self, op, a, b):
        ops = {"add": add, "sub": subtract, "mul": multiply, "div": divide}
        if op not in ops:
            raise ValueError(f"unknown op: {op!r}")
        result = ops[op](a, b)
        self.history.append((op, a, b, result))
        return result

    def last(self):
        return self.history[-1] if self.history else None
