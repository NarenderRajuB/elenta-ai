# Tests for the configuration boundary (app/config.py).
#
# Organised by requirement. Each requirement has positive cases (valid input is
# accepted), negative cases (invalid input is rejected with a clear error) and
# edge cases (boundary or unusual input whose behaviour is a deliberate choice).
# The scenario list is mirrored in README.md.

import re
from dataclasses import fields
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent
VALID = {"LLM_URL": "http://host.docker.internal:11434/v1", "LLM_MODEL": "qwen2.5:0.5b"}


def _env_example() -> dict[str, str]:
    """Parse .env.example as KEY=VALUE lines, ignoring comments and blanks."""
    result = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# REQ-013: all application configuration comes from environment variables
# ---------------------------------------------------------------------------

class TestReq013EnvOnly:
    def test_positive_settings_built_from_env_mapping(self):
        settings = load_settings(VALID)
        assert settings == Settings(llm_url=VALID["LLM_URL"], llm_model=VALID["LLM_MODEL"])

    def test_negative_no_hidden_read_of_process_environment(self, monkeypatch):
        # Even if the real process env is valid, an empty mapping must fail:
        # proves load_settings reads only what it is given.
        monkeypatch.setenv("LLM_URL", VALID["LLM_URL"])
        monkeypatch.setenv("LLM_MODEL", VALID["LLM_MODEL"])
        with pytest.raises(ConfigError):
            load_settings({})

    def test_negative_no_other_module_reads_environment(self):
        # The config module is the single place env vars are read.
        pattern = re.compile(r"\bos\.environ\b|\bgetenv\(")
        offenders = [
            p.relative_to(ROOT).as_posix()
            for p in (ROOT / "app").rglob("*.py")
            if p.name != "config.py" and pattern.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_edge_unrelated_variables_are_ignored(self):
        settings = load_settings({**VALID, "PATH": "/usr/bin", "LLM_UNKNOWN": "x"})
        assert settings.llm_model == VALID["LLM_MODEL"]

    def test_edge_settings_are_immutable(self):
        settings = load_settings(VALID)
        with pytest.raises(AttributeError):
            settings.llm_model = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# REQ-014: .env.example is a complete reference with safe example values
# ---------------------------------------------------------------------------

class TestReq014EnvExample:
    def test_positive_lists_exactly_the_settings_fields(self):
        assert set(_env_example()) == {f.name.upper() for f in fields(Settings)}

    def test_positive_example_values_are_themselves_valid(self):
        # Copying .env.example to .env must give a working configuration.
        load_settings(_env_example())

    def test_negative_example_url_contains_no_credentials(self):
        assert "@" not in _env_example()["LLM_URL"]

    def test_edge_no_duplicate_keys(self):
        keys = [
            line.partition("=")[0].strip()
            for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# REQ-015: LLM_URL and LLM_MODEL drive the client; switching needs no code change
# ---------------------------------------------------------------------------

class TestReq015EndpointSwitching:
    @pytest.mark.parametrize(
        "url, model",
        [
            ("http://host.docker.internal:11434/v1", "qwen2.5:0.5b"),            # Ollama on host
            ("http://model-runner.docker.internal/engines/v1", "ai/example:Q4_K_M"),  # DMR
            ("https://llm.internal.example/v1", "example-model"),               # any compatible endpoint
        ],
    )
    def test_positive_any_compatible_endpoint_by_env_only(self, url, model):
        settings = load_settings({"LLM_URL": url, "LLM_MODEL": model})
        assert (settings.llm_url, settings.llm_model) == (url, model)

    def test_edge_model_identifier_kept_verbatim(self):
        # Slashes, colons and case are meaningful to providers; never rewrite them.
        model = "ai/Qwen2.5:0.5B-Q4_K_M"
        assert load_settings({**VALID, "LLM_MODEL": model}).llm_model == model

    def test_edge_surrounding_whitespace_trimmed(self):
        settings = load_settings({"LLM_URL": "  http://h:1/v1  ", "LLM_MODEL": "  qwen2.5:0.5b\t"})
        assert (settings.llm_url, settings.llm_model) == ("http://h:1/v1", "qwen2.5:0.5b")

    @pytest.mark.parametrize("raw", ["http://h/v1/", "http://h/v1//"])
    def test_edge_trailing_slashes_normalised(self, raw):
        assert load_settings({**VALID, "LLM_URL": raw}).llm_url == "http://h/v1"

    def test_edge_ipv6_host_accepted(self):
        assert load_settings({**VALID, "LLM_URL": "http://[::1]:11434/v1"}).llm_url == "http://[::1]:11434/v1"

    def test_edge_uppercase_scheme_accepted(self):
        load_settings({**VALID, "LLM_URL": "HTTP://Host:11434/v1"})


# ---------------------------------------------------------------------------
# REQ-016: fail fast with a clear error on missing or invalid configuration
# ---------------------------------------------------------------------------

class TestReq016FailFast:
    def test_positive_valid_config_raises_nothing(self):
        load_settings(VALID)

    @pytest.mark.parametrize("missing", ["LLM_URL", "LLM_MODEL"])
    def test_negative_each_required_variable_missing(self, missing):
        env = {k: v for k, v in VALID.items() if k != missing}
        with pytest.raises(ConfigError, match=f"{missing} is required"):
            load_settings(env)

    @pytest.mark.parametrize("value", ["", "   ", "\t\n"])
    def test_negative_empty_or_blank_counts_as_missing(self, value):
        with pytest.raises(ConfigError, match="LLM_MODEL is required"):
            load_settings({**VALID, "LLM_MODEL": value})

    @pytest.mark.parametrize("bad_url", ["localhost:11434", "ftp://host/v1", "http://", "not a url", "//host/v1"])
    def test_negative_url_without_http_scheme_or_host(self, bad_url):
        with pytest.raises(ConfigError, match="must be an http"):
            load_settings({**VALID, "LLM_URL": bad_url})

    @pytest.mark.parametrize("bad_url", ["http://host:99999/v1", "http://host:abc/v1", "http://host:-1/v1"])
    def test_negative_invalid_port(self, bad_url):
        # Regression for TS-002: urlsplit only validates the port when .port is read.
        with pytest.raises(ConfigError, match="invalid port"):
            load_settings({**VALID, "LLM_URL": bad_url})

    @pytest.mark.parametrize("bad_url", ["http://h/v1?x=1", "http://h/v1#frag"])
    def test_negative_query_or_fragment(self, bad_url):
        # Regression for TS-002: appending /chat/completions would build a wrong URL.
        with pytest.raises(ConfigError, match="without a query string or fragment"):
            load_settings({**VALID, "LLM_URL": bad_url})

    def test_negative_all_problems_reported_together(self):
        # One error listing everything avoids a fix-one-restart-repeat loop.
        with pytest.raises(ConfigError) as exc:
            load_settings({})
        assert len(exc.value.problems) == 2
        assert "LLM_URL" in str(exc.value) and "LLM_MODEL" in str(exc.value)
        assert "2 problem(s)" in str(exc.value)

    def test_negative_invalid_url_and_missing_model_reported_together(self):
        with pytest.raises(ConfigError) as exc:
            load_settings({"LLM_URL": "ftp://x"})
        assert len(exc.value.problems) == 2

    def test_edge_error_does_not_echo_url_value(self):
        # REQ-068: URLs may carry credentials and errors end up in logs.
        for bad in ("ftp://user:s3cret@host", "http://user:s3cret@host:99999/v1"):
            with pytest.raises(ConfigError) as exc:
                load_settings({**VALID, "LLM_URL": bad})
            assert "s3cret" not in str(exc.value)

    @pytest.mark.parametrize("port", ["0", "65535"])
    def test_edge_port_range_boundaries_accepted(self, port):
        load_settings({**VALID, "LLM_URL": f"http://host:{port}/v1"})
