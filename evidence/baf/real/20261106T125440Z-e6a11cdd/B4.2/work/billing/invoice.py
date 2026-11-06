from .totals import compute_total

def invoice_amount(items):
    return compute_total([item["price"] for item in items], tax_rate=0.2)

