import pytest

from mangarr.settings_service import validate


class TestNamingTemplates:
    def test_default_templates_pass(self):
        validate({
            "naming_template": "{series} - Ch. {chapter:04.1f}",
            "naming_template_no_volume": "{series} - Ch. {chapter:04.1f}",
        })

    def test_all_placeholders_pass(self):
        validate({"naming_template": "{series} v{volume} c{chapter} {title}"})

    def test_unknown_placeholder_rejected(self):
        # the live failure: this used to be stored, then crash the queue
        # worker on every download attempt
        with pytest.raises(ValueError, match="naming_template"):
            validate({"naming_template": "{series} - Ch. {chaptr}"})

    def test_bad_format_spec_rejected(self):
        with pytest.raises(ValueError):
            validate({"naming_template_no_volume": "{chapter:zz}"})

    def test_unrelated_keys_ignored(self):
        validate({"qbittorrent_url": "http://localhost:8080"})


class TestMonitorInterval:
    def test_valid(self):
        validate({"monitor_interval_minutes": "60"})

    def test_non_numeric_rejected(self):
        # a stored non-numeric value used to abort scheduler startup
        with pytest.raises(ValueError, match="monitor_interval_minutes"):
            validate({"monitor_interval_minutes": "abc"})

    def test_zero_rejected(self):
        with pytest.raises(ValueError, match="at least 1"):
            validate({"monitor_interval_minutes": "0"})
