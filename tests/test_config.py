from ledgersync.config import Settings, read_env_file


def test_defaults_are_local_and_bounded():
    s = Settings.from_env({})
    assert s.host == "127.0.0.1"
    assert s.cors_origins == ("http://localhost:3000", "http://127.0.0.1:3000")
    assert s.reload is False and s.warmup is True
    assert s.max_upload_bytes == 20 * 1024 * 1024
    assert s.max_pdf_pages == 30


def test_env_overrides():
    s = Settings.from_env({
        "LEDGERSYNC_CORS_ORIGINS": "http://a:1, http://b:2",
        "LEDGERSYNC_RELOAD": "true",
        "LEDGERSYNC_WARMUP": "0",
        "LEDGERSYNC_MAX_UPLOAD_MB": "5",
    })
    assert s.cors_origins == ("http://a:1", "http://b:2")
    assert s.reload is True and s.warmup is False
    assert s.max_upload_bytes == 5 * 1024 * 1024


def test_groq_settings_have_working_defaults():
    s = Settings.from_env({})
    assert (s.groq_base_url, s.groq_model) == ("https://api.groq.com/openai/v1", "qwen/qwen3.8-27b")
    assert (s.groq_timeout, s.groq_max_output_tokens, s.groq_reasoning_effort, s.groq_max_images) == (
        60.0, 8192, "low", 1)
    assert (s.groq_api_key, s.business_name, s.health_ttl) == ("", "", 300.0)


def test_groq_settings_use_groqs_own_names():
    s = Settings.from_env({
        "GROQ_API_KEY": " gsk-test ", "GROQ_MODEL": "some/model", "GROQ_BASE_URL": "https://example.test/v1/",
        "GROQ_REASONING_EFFORT": "", "LEDGERSYNC_BUSINESS_NAME": "Northbridge Consulting Ltd",
    })
    assert (s.groq_api_key, s.groq_model, s.groq_base_url) == ("gsk-test", "some/model", "https://example.test/v1")
    assert (s.groq_reasoning_effort, s.business_name) == ("", "Northbridge Consulting Ltd")


def test_the_api_key_is_never_printed():
    assert "gsk-secret" not in repr(Settings(groq_api_key="gsk-secret"))


def test_env_file_values_are_used_but_real_environment_variables_win(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\n\nexport GROQ_API_KEY="gsk-from-file"\n'
                        "GROQ_MODEL='file/model'\nnot a setting\n")
    monkeypatch.setenv("GROQ_MODEL", "env/model")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    s = Settings.from_env(env_file=env_file)
    assert (s.groq_api_key, s.groq_model) == ("gsk-from-file", "env/model")


def test_a_missing_env_file_gives_no_values(tmp_path):
    assert read_env_file(tmp_path / "absent.env") == {}


def test_four_files_are_read_at_once_unless_set_otherwise():
    assert Settings.from_env({}).max_parallel_jobs == 4
    assert Settings.from_env({"LEDGERSYNC_MAX_PARALLEL_JOBS": "1"}).max_parallel_jobs == 1
    assert Settings.from_env({"LEDGERSYNC_MAX_PARALLEL_JOBS": "0"}).max_parallel_jobs == 1   # never below one
