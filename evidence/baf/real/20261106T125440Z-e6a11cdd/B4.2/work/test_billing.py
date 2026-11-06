import unittest
from billing import compute_total
from billing.invoice import invoice_amount

class BillingTests(unittest.TestCase):
    def test_total(self):
        self.assertEqual(compute_total([10, 5]), 15)

    def test_invoice(self):
        self.assertEqual(invoice_amount([{"price": 10}]), 12)

