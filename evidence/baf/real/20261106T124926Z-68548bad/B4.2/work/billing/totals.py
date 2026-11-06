def compute_total(prices, tax_rate=0):
    subtotal = sum(prices)
    return round(subtotal * (1 + tax_rate), 2)
