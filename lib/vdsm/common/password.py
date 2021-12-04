#
# Copyright 2015-2021 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Refer to the README and COPYING files for full details of the license
#

import copy

import six


class ProtectedPassword(object):
    """
    Protect a password so it will not be logged or serialized by mistake.
    """
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return type(self) == type(other) and self.value == other.value

    def __ne__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        return "********"

    def __repr__(self):
        return repr(str(self))

    def __hash__(self):
        return hash((self.__class__, self.value))


def protect_passwords(obj):
    """
    Replace "password" values with ProtectedPassword() object.

    Accept a dict, list of dicts or nested structure containing these types.
    """
    # Beware of revealing passwords in unintended places!
    obj = copy.deepcopy(obj)
    for d, key, value in _walk(obj):
        d[key] = ProtectedPassword(value)
    return obj


def unprotect_passwords(obj):
    """
    Replace ProtectedPassword() objects with the actual password value.

    Accept a dict, list of dicts or nested structure containing these types.
    """
    for d, key, value in _walk(obj):
        if isinstance(value, ProtectedPassword):
            d[key] = value.value
    return obj


def unprotect(obj):
    """
    If obj is a protected password, return the protected value. Otherwise
    returns obj.
    """
    if isinstance(obj, ProtectedPassword):
        return obj.value
    return obj


def _walk(obj):
    if isinstance(obj, dict):
        for key, value in six.iteritems(obj):
            if key == "password" or \
               isinstance(key, str) and key.startswith("_X_"):
                yield obj, key, value
            elif isinstance(value, (dict, list)):
                for d, k, v in _walk(value):
                    yield d, k, v
    elif isinstance(obj, list):
        for item in obj:
            for d, k, v in _walk(item):
                yield d, k, v
