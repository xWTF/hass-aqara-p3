# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Protocol profiles are ordinary, reviewable Python modules; no dynamic exec."""

from .daikin_p3 import DaikinP3

PROFILES = {"daikin_p3": DaikinP3}
