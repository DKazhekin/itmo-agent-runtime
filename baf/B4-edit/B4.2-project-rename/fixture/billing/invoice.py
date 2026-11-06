from .totals import calc_total

def invoice_amount(items):
    return calc_total([item["price"] for item in items], tax_rate=0.2)
