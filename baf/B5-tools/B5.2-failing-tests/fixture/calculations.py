def add(left, right):
    return left + right

def discounted(price, discount):
    return price - abs(discount)

def average(values):
    return sum(values) / max(len(values), 1)

def rounded_cents(value):
    return int(value * 100)
