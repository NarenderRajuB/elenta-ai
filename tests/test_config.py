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


# ---------------------------------------------------------------------------
# Optional settings (APP_HOST, APP_PORT, LLM_HEALTH_TIMEOUT_SECONDS):
# REQ-013 (from env), REQ-016 (invalid values fail fast), REQ-017 (probe timeout)
# ---------------------------------------------------------------------------

class TestOptionalSettings:
    def test_positive_defaults_when_unset(self):
        s = load_settings(VALID)
        assert (s.app_host, s.app_port, s.llm_health_timeout_seconds) == ("127.0.0.1", 8000, 3.0)

    def test_positive_overrides_from_env(self):
        s = load_settings({**VALID, "APP_HOST": "0.0.0.0", "APP_PORT": "9000", "LLM_HEALTH_TIMEOUT_SECONDS": "1.5"})
        assert (s.app_host, s.app_port, s.llm_health_timeout_seconds) == ("0.0.0.0", 9000, 1.5)

    def test_positive_process_env_loader_reads_os_environ(self, monkeypatch):
        monkeypatch.setenv("LLM_URL", VALID["LLM_URL"])
        monkeypatch.setenv("LLM_MODEL", VALID["LLM_MODEL"])
        monkeypatch.setenv("APP_PORT", "8123")
        from app.config import load_settings_from_process_env
        assert load_settings_from_process_env().app_port == 8123

    @pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "8000.5", "80 80"])
    def test_negative_invalid_app_port(self, port):
        with pytest.raises(ConfigError, match="APP_PORT must be a whole number from 1 to 65535"):
            load_settings({**VALID, "APP_PORT": port})

    @pytest.mark.parametrize("timeout", ["0", "-1", "abc", "nan", "inf", "-inf"])
    def test_negative_invalid_health_timeout(self, timeout):
        with pytest.raises(ConfigError, match="LLM_HEALTH_TIMEOUT_SECONDS must be a positive number"):
            load_settings({**VALID, "LLM_HEALTH_TIMEOUT_SECONDS": timeout})

    def test_negative_optional_and_required_problems_reported_together(self):
        with pytest.raises(ConfigError) as exc:
            load_settings({"APP_PORT": "0", "LLM_HEALTH_TIMEOUT_SECONDS": "nan"})
        assert len(exc.value.problems) == 4

    @pytest.mark.parametrize("name", ["APP_HOST", "APP_PORT", "LLM_HEALTH_TIMEOUT_SECONDS"])
    def test_edge_blank_optional_value_uses_default(self, name):
        s = load_settings({**VALID, name: "   "})
        assert s == load_settings(VALID)

    @pytest.mark.parametrize("port, expected", [("1", 1), ("65535", 65535), (" 8080 ", 8080)])
    def test_edge_app_port_boundaries_and_whitespace(self, port, expected):
        assert load_settings({**VALID, "APP_PORT": port}).app_port == expected

    @pytest.mark.parametrize("timeout, expected", [("0.001", 0.001), ("10", 10.0), ("1e1", 10.0)])
    def test_edge_health_timeout_small_and_scientific(self, timeout, expected):
        assert load_settings({**VALID, "LLM_HEALTH_TIMEOUT_SECONDS": timeout}).llm_health_timeout_seconds == expected


# ---------------------------------------------------------------------------
# Corpus settings (CORPUS_DIR, CORPUS_MAX_FILE_BYTES, CORPUS_MAX_FILES,
# CORPUS_SETTLE_SECONDS): REQ-013, REQ-016, REQ-041 limits, REQ-045 settle window
# ---------------------------------------------------------------------------

class TestCorpusSettings:
    def test_positive_defaults(self):
        s = load_settings(VALID)
        assert (s.corpus_dir, s.corpus_max_file_bytes, s.corpus_max_files, s.corpus_settle_seconds) == (
            "/data", 50 * 1024 * 1024, 500, 0.5)

    def test_positive_overrides(self):
        s = load_settings({**VALID, "CORPUS_DIR": "/srv/docs", "CORPUS_MAX_FILE_BYTES": "2048",
                           "CORPUS_MAX_FILES": "10", "CORPUS_SETTLE_SECONDS": "2"})
        assert (s.corpus_dir, s.corpus_max_file_bytes, s.corpus_max_files, s.corpus_settle_seconds) == (
            "/srv/docs", 2048, 10, 2.0)

    def test_positive_relative_corpus_dir_made_absolute(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert load_settings({**VALID, "CORPUS_DIR": "./data"}).corpus_dir == str(tmp_path / "data")

    @pytest.mark.parametrize("name", ["CORPUS_MAX_FILE_BYTES", "CORPUS_MAX_FILES"])
    @pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", "1e6"])
    def test_negative_invalid_limits(self, name, value):
        with pytest.raises(ConfigError, match=f"{name} must be a whole number of at least 1"):
            load_settings({**VALID, name: value})

    @pytest.mark.parametrize("value", ["-0.1", "abc", "nan", "inf"])
    def test_negative_invalid_settle_seconds(self, value):
        with pytest.raises(ConfigError, match="CORPUS_SETTLE_SECONDS must be zero or a positive number"):
            load_settings({**VALID, "CORPUS_SETTLE_SECONDS": value})

    def test_edge_settle_zero_allowed(self):
        assert load_settings({**VALID, "CORPUS_SETTLE_SECONDS": "0"}).corpus_settle_seconds == 0.0

    def test_edge_limit_of_one_allowed(self):
        s = load_settings({**VALID, "CORPUS_MAX_FILE_BYTES": "1", "CORPUS_MAX_FILES": "1"})
        assert (s.corpus_max_file_bytes, s.corpus_max_files) == (1, 1)

    @pytest.mark.parametrize("name", ["CORPUS_DIR", "CORPUS_MAX_FILE_BYTES", "CORPUS_MAX_FILES", "CORPUS_SETTLE_SECONDS"])
    def test_edge_blank_uses_default(self, name):
        assert load_settings({**VALID, name: "  "}) == load_settings(VALID)

    def test_edge_loading_settings_does_not_touch_filesystem(self):
        # A non-existent CORPUS_DIR is accepted here; existence is checked at startup.
        assert load_settings({**VALID, "CORPUS_DIR": "/definitely/not/here"}).corpus_dir == "/definitely/not/here"


# ---------------------------------------------------------------------------
# Selection settings (CHUNK_MAX_CHARS, CONTEXT_TOKEN_BUDGET, SELECTION_MIN_SCORE):
# REQ-055 explicit budget, REQ-050 selection, REQ-016 fail fast
# ---------------------------------------------------------------------------

class TestSelectionSettings:
    def test_positive_defaults(self):
        s = load_settings(VALID)
        assert (s.chunk_max_chars, s.context_token_budget, s.selection_min_score) == (800, 1500, 0.0)

    def test_positive_overrides(self):
        s = load_settings({**VALID, "CHUNK_MAX_CHARS": "400", "CONTEXT_TOKEN_BUDGET": "900", "SELECTION_MIN_SCORE": "1.25"})
        assert (s.chunk_max_chars, s.context_token_budget, s.selection_min_score) == (400, 900, 1.25)

    @pytest.mark.parametrize("name", ["CHUNK_MAX_CHARS", "CONTEXT_TOKEN_BUDGET"])
    @pytest.mark.parametrize("value", ["0", "-5", "abc", "2.5"])
    def test_negative_invalid_sizes(self, name, value):
        with pytest.raises(ConfigError, match=f"{name} must be a whole number of at least 1"):
            load_settings({**VALID, name: value})

    @pytest.mark.parametrize("value", ["-0.1", "abc", "nan", "inf"])
    def test_negative_invalid_min_score(self, value):
        with pytest.raises(ConfigError, match="SELECTION_MIN_SCORE must be zero or a positive number"):
            load_settings({**VALID, "SELECTION_MIN_SCORE": value})

    @pytest.mark.parametrize("name", ["CHUNK_MAX_CHARS", "CONTEXT_TOKEN_BUDGET", "SELECTION_MIN_SCORE"])
    def test_edge_blank_uses_default(self, name):
        assert load_settings({**VALID, name: ""}) == load_settings(VALID)

    def test_edge_minimums_accepted(self):
        s = load_settings({**VALID, "CHUNK_MAX_CHARS": "1", "CONTEXT_TOKEN_BUDGET": "1", "SELECTION_MIN_SCORE": "0"})
        assert (s.chunk_max_chars, s.context_token_budget, s.selection_min_score) == (1, 1, 0.0)


# ---------------------------------------------------------------------------
# Context accounting (LLM_CONTEXT_TOKENS, LLM_MAX_TOKENS): REQ-055, REQ-071, REQ-016
# ---------------------------------------------------------------------------

class TestContextSettings:
    def test_positive_defaults_match_ollama_pin(self):
        s = load_settings(VALID)
        assert (s.llm_context_tokens, s.llm_max_tokens) == (4096, 512)

    def test_positive_overrides(self):
        s = load_settings({**VALID, "LLM_CONTEXT_TOKENS": "8192", "LLM_MAX_TOKENS": "1024"})
        assert (s.llm_context_tokens, s.llm_max_tokens) == (8192, 1024)

    @pytest.mark.parametrize("name", ["LLM_CONTEXT_TOKENS", "LLM_MAX_TOKENS"])
    @pytest.mark.parametrize("value", ["0", "-1", "abc", "4096.5"])
    def test_negative_invalid_numbers(self, name, value):
        with pytest.raises(ConfigError, match=f"{name} must be a whole number of at least 1"):
            load_settings({**VALID, name: value})

    def test_negative_answer_allowance_not_below_context(self):
        with pytest.raises(ConfigError, match="LLM_MAX_TOKENS must be smaller than LLM_CONTEXT_TOKENS"):
            load_settings({**VALID, "LLM_CONTEXT_TOKENS": "1000", "LLM_MAX_TOKENS": "1000"})

    def test_negative_evidence_budget_not_below_context(self):
        with pytest.raises(ConfigError, match="CONTEXT_TOKEN_BUDGET must be smaller than LLM_CONTEXT_TOKENS"):
            load_settings({**VALID, "LLM_CONTEXT_TOKENS": "1500"})

    def test_edge_just_below_context_accepted(self):
        s = load_settings({**VALID, "LLM_CONTEXT_TOKENS": "1501", "LLM_MAX_TOKENS": "1500"})
        assert (s.llm_context_tokens, s.llm_max_tokens) == (1501, 1500)
