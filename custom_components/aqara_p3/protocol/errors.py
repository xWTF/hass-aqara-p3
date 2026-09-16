# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors


class P3Error(Exception):
    """Base class with messages safe to show in the application log."""


class AuthenticationError(P3Error):
    pass


class ConnectionLost(P3Error):
    pass


class InvalidData(P3Error):
    pass


class ResourceMissing(InvalidData):
    """An optional firmware cache has not been created yet."""


class UnsupportedDevice(P3Error):
    pass


class UnsupportedState(P3Error):
    pass
