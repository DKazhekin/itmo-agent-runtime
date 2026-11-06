"""Behavior tests after verifying that submitted code has the expected AST."""

def verify(compute_total):
    cases = [([], 0, 0), ([1, 2, 3], 0, 6), ([10], 0.2, 12), ([0.1, 0.2], 0, 0.3), ([-5, 10], 0, 5), ([19.99, 2.51], 0.2, 27.0)]
    for prices, tax, expected in cases:
        actual = compute_total(prices, tax)
        if actual != expected:
            raise ValueError(f"Behavior mismatch for {prices}: {actual} != {expected}")
