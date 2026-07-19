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


class TestAutomaticTorrentLimits:
    def test_defaults_are_valid(self):
        validate({
            "torrent_auto_max_size_gib": "30",
            "torrent_auto_min_seeders": "1",
        })

    @pytest.mark.parametrize("value", ["0", "-1", "nope"])
    def test_size_limit_must_be_positive_integer(self, value):
        with pytest.raises(ValueError, match="torrent_auto_max_size_gib"):
            validate({"torrent_auto_max_size_gib": value})

    def test_zero_seeders_can_be_explicitly_allowed(self):
        validate({"torrent_auto_min_seeders": "0"})


class TestContentProxy:
    def test_http_proxy_with_selected_source_is_valid(self):
        validate({
            "download_proxy_url": "http://192.168.1.28:8888",
            "source_mangadex_proxy_enabled": "true",
        })

    def test_selected_source_requires_proxy_url(self):
        with pytest.raises(ValueError, match="download_proxy_url is required"):
            validate({"source_mangadex_proxy_enabled": "true"})

    def test_disabled_source_does_not_require_proxy_url(self):
        validate({
            "source_mangadex_enabled": "false",
            "source_mangadex_proxy_enabled": "true",
        })

    @pytest.mark.parametrize("url", ["192.168.1.28:8888", "socks5://localhost:1080"])
    def test_proxy_url_requires_supported_scheme(self, url):
        with pytest.raises(ValueError, match="valid http"):
            validate({"download_proxy_url": url})

    def test_no_proxy_url_is_valid_when_all_sources_are_direct(self):
        validate({
            "download_proxy_url": "",
            "source_mangadex_proxy_enabled": "false",
        })
