"""Mask customer numbers (KU, PPE) in log messages.

The add-on log is often pasted into issues; the contract account number (KU,
11 digits) and the meter point number (PPE, 18 digits) are personal data.
Logs keep only their last 4 digits, enough to tell contracts apart.
"""
from __future__ import annotations

import re

# KU and PPE are long digit runs; dates, hours and sums in log lines are not.
_CUSTOMER_NUMBER = re.compile(r"\d{8,}")


def redact(value: object) -> str:
    """str(value) with every run of 8+ digits shortened to ***<last 4 digits>."""
    return _CUSTOMER_NUMBER.sub(lambda m: "***" + m.group()[-4:], str(value))
