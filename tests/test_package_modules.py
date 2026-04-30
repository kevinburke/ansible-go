from __future__ import annotations

import unittest

from plugins.modules import apt, dnf


class TestAptModuleCompatibilityPreflight(unittest.TestCase):
    def _params(self, **overrides):
        params = {
            name: default
            for name, default in apt._UNSUPPORTED_DEFAULTS.items()
        }
        params.update(
            {
                "name": ["curl"],
                "state": "present",
                "update_cache": False,
                "cache_valid_time": 0,
            }
        )
        params.update(overrides)
        return params

    def test_default_args_are_fast_path_compatible(self) -> None:
        self.assertEqual(apt._unsupported_params(self._params()), [])

    def test_purge_is_rejected_before_running_apt_get(self) -> None:
        self.assertEqual(apt._unsupported_params(self._params(purge=True)), ["purge"])

    def test_version_spec_is_rejected_before_running_apt_get(self) -> None:
        self.assertEqual(
            apt._unsupported_params(self._params(name=["curl=1.2.3"])),
            ["name"],
        )


class TestDnfModuleCompatibilityPreflight(unittest.TestCase):
    def _params(self, **overrides):
        params = {
            name: default
            for name, default in dnf._UNSUPPORTED_DEFAULTS.items()
        }
        params.update(
            {
                "name": ["curl"],
                "state": "present",
            }
        )
        params.update(overrides)
        return params

    def test_default_args_are_fast_path_compatible(self) -> None:
        self.assertEqual(dnf._unsupported_params(self._params()), [])

    def test_update_cache_is_rejected_before_running_dnf(self) -> None:
        self.assertEqual(
            dnf._unsupported_params(self._params(update_cache=True)),
            ["update_cache"],
        )

    def test_rpm_path_is_rejected_before_running_dnf(self) -> None:
        self.assertEqual(
            dnf._unsupported_params(self._params(name=["/tmp/pkg.rpm"])),
            ["name"],
        )


if __name__ == "__main__":
    unittest.main()
