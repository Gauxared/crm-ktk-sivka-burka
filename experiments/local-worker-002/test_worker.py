"""Unit tests for the isolated cash summary benchmark."""

import importlib.util
import os
import unittest


def _load_module():
    path = os.path.join(os.path.dirname(__file__), "money_summary.py")
    spec = importlib.util.spec_from_file_location("money_summary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ms = _load_module()


class TestSummarizeCash(unittest.TestCase):
    def test_exact_integer_subclasses_rejected(self):
        class IntegerSubclass(int):
            pass
        for args in [(IntegerSubclass(1), [], []), (None, [IntegerSubclass(1)], []),
                     (None, [], [IntegerSubclass(1)])]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                ms.summarize_cash(*args)

    def test_valid_basic(self):
        result = ms.summarize_cash(700000, [350000, 350000], [])
        self.assertEqual(result, {
            "received_minor": 700000,
            "returned_minor": 0,
            "net_received_minor": 700000,
            "balance_minor": 0,
            "warning": None,
        })

    def test_valid_total_none(self):
        result = ms.summarize_cash(None, [100], [])
        self.assertEqual(result, {
            "received_minor": 100,
            "returned_minor": 0,
            "net_received_minor": 100,
            "balance_minor": None,
            "warning": None,
        })

    def test_valid_overpayment(self):
        result = ms.summarize_cash(700, [750], [])
        self.assertEqual(result, {
            "received_minor": 750,
            "returned_minor": 0,
            "net_received_minor": 750,
            "balance_minor": -50,
            "warning": None,
        })

    def test_valid_refund_exceeds_receipt(self):
        result = ms.summarize_cash(700, [100], [200])
        self.assertEqual(result, {
            "received_minor": 100,
            "returned_minor": 200,
            "net_received_minor": -100,
            "balance_minor": 800,
            "warning": "DATA_REVIEW_REQUIRED",
        })

    def test_valid_zero_total_empty_lists(self):
        result = ms.summarize_cash(0, [], [])
        self.assertEqual(result, {
            "received_minor": 0,
            "returned_minor": 0,
            "net_received_minor": 0,
            "balance_minor": 0,
            "warning": None,
        })

    def test_valid_large_values(self):
        result = ms.summarize_cash(9000000000000, [9000000000000, 9000000000000], [])
        self.assertEqual(result, {
            "received_minor": 18000000000000,
            "returned_minor": 0,
            "net_received_minor": 18000000000000,
            "balance_minor": -9000000000000,
            "warning": None,
        })

    def test_invalid_total_bool(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(True, [], [])

    def test_invalid_total_negative(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(-1, [], [])

    def test_invalid_total_too_large(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(9000000000001, [], [])

    def test_invalid_total_float(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(1.5, [], [])

    def test_invalid_total_string(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash("1", [], [])

    def test_invalid_receipt_bool(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(None, [True], [])

    def test_invalid_receipt_zero(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(None, [0], [])

    def test_invalid_receipt_negative(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(None, [-1], [])

    def test_invalid_receipt_float(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(None, [1.5], [])

    def test_invalid_refund_string(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(None, [], ["2"])

    def test_invalid_refund_too_large(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(None, [], [9000000000001])

    def test_invalid_receipts_tuple(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(None, (1,), [])

    def test_invalid_refunds_none(self):
        with self.assertRaises(ValueError):
            ms.summarize_cash(None, [], None)

    def test_input_immutability(self):
        receipts = [1, 2]
        refunds = [1]
        ms.summarize_cash(5, receipts, refunds)
        self.assertEqual(receipts, [1, 2])
        self.assertEqual(refunds, [1])


if __name__ == "__main__":
    unittest.main()
