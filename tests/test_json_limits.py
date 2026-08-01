"""General strict-JSON numeric bounds shared by every dmcheck transport."""

import unittest

from dmcheck.validation import (InputValidationError,
                                MAX_JSON_NUMBER_CHARACTERS,
                                parse_json_value)


class TestJSONNumericLimits(unittest.TestCase):
    def test_integer_tokens_have_a_runtime_independent_bound(self):
        accepted = "9" * MAX_JSON_NUMBER_CHARACTERS
        self.assertEqual(parse_json_value(accepted, "/value"), int(accepted))

        with self.assertRaises(InputValidationError) as caught:
            parse_json_value("9" * (MAX_JSON_NUMBER_CHARACTERS + 1),
                             "/value")
        self.assertEqual(
            {problem.code for problem in caught.exception.issues},
            {"input.invalid_json"},
        )

    def test_float_tokens_share_the_same_bound(self):
        accepted = "0." + "1" * (MAX_JSON_NUMBER_CHARACTERS - 2)
        self.assertIsInstance(parse_json_value(accepted, "/value"), float)

        with self.assertRaises(InputValidationError):
            parse_json_value(
                "0." + "1" * (MAX_JSON_NUMBER_CHARACTERS - 1), "/value")

        with self.assertRaises(InputValidationError):
            parse_json_value("1e999999", "/value")


if __name__ == "__main__":
    unittest.main()
