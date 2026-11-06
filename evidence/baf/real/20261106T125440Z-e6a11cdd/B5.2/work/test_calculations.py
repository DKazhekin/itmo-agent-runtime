import unittest
from calculations import add, discounted, average, rounded_cents

class CalculationTests(unittest.TestCase):
    def test_add_00(self):
        self.assertEqual(add(0, 2), 2)

    def test_add_01(self):
        self.assertEqual(add(1, 2), 3)

    def test_add_02(self):
        self.assertEqual(add(2, 2), 4)

    def test_add_03(self):
        self.assertEqual(add(3, 2), 5)

    def test_add_04(self):
        self.assertEqual(add(4, 2), 6)

    def test_add_05(self):
        self.assertEqual(add(5, 2), 7)

    def test_add_06(self):
        self.assertEqual(add(6, 2), 8)

    def test_add_07(self):
        self.assertEqual(add(7, 2), 9)

    def test_add_08(self):
        self.assertEqual(add(8, 2), 10)

    def test_add_09(self):
        self.assertEqual(add(9, 2), 11)

    def test_add_10(self):
        self.assertEqual(add(10, 2), 12)

    def test_add_11(self):
        self.assertEqual(add(11, 2), 13)

    def test_add_12(self):
        self.assertEqual(add(12, 2), 14)

    def test_add_13(self):
        self.assertEqual(add(13, 2), 15)

    def test_add_14(self):
        self.assertEqual(add(14, 2), 16)

    def test_add_15(self):
        self.assertEqual(add(15, 2), 17)

    def test_add_16(self):
        self.assertEqual(add(16, 2), 18)

    def test_negative_discount(self):
        self.assertEqual(discounted(100, -10), 110)

    def test_empty_average(self):
        with self.assertRaises(ValueError):
            average([])

    def test_cent_rounding(self):
        self.assertEqual(rounded_cents(1.239), 124)
