"""環境変数名とコードの対応のテスト（.env に書く名前が実際に読み込まれることを確認する）。"""
import pytest

from app.config import Settings
from app.providers import fred

ALL_KEYS = ["DATABASE_URL", "JQUANTS_API_KEY", "JQUANTS_PLAN", "JQUANTS_REFRESH_TOKEN", "US_PRICE_PROVIDER", "TIINGO_API_KEY",
            "MASSIVE_API_KEY", "POLYGON_API_KEY", "MASSIVE_BASE_URL", "POLYGON_BASE_URL", "SEC_USER_AGENT",
            "SEC_UNIVERSE_SIZE", "SEC_EXTRA_TICKERS", "FRED_API_KEY", "EDINET_API_KEY", "ANTHROPIC_API_KEY"]


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def make(**kw):
    return Settings(_env_file=None, **kw) if kw else Settings(_env_file=None)


def test_env_names_are_read(clean_env):
    env = {"JQUANTS_API_KEY": "jq", "JQUANTS_PLAN": "Light", "US_PRICE_PROVIDER": "Tiingo", "TIINGO_API_KEY": "ti",
           "SEC_USER_AGENT": "AI-STOCK-INTELLIGENCE/0.3 me@mydomain.jp", "SEC_UNIVERSE_SIZE": "300",
           "SEC_EXTRA_TICKERS": "KO,PEP", "FRED_API_KEY": "fr", "EDINET_API_KEY": "ed", "ANTHROPIC_API_KEY": "an",
           "MASSIVE_API_KEY": "ma", "MASSIVE_BASE_URL": "https://api.massive.com"}
    for k, v in env.items():
        clean_env.setenv(k, v)
    s = make()
    assert (s.jquants_api_key, s.jquants_plan, s.us_price_provider, s.tiingo_api_key) == ("jq", "light", "tiingo", "ti")
    assert (s.fred_api_key, s.edinet_api_key, s.anthropic_api_key, s.massive_api_key) == ("fr", "ed", "an", "ma")
    assert s.massive_base_url == "https://api.massive.com" and s.sec_universe_size == 300 and s.sec_extra_tickers == "KO,PEP"
    assert s.sec_ua_is_provisional is False


def test_polygon_name_used_even_if_empty_massive_line_in_env_file(clean_env, tmp_path):
    f = tmp_path / ".env"
    f.write_text("US_PRICE_PROVIDER=polygon\nMASSIVE_API_KEY=\nPOLYGON_API_KEY=pk\n", encoding="utf-8")
    s = Settings(_env_file=(f,))
    assert s.us_price_provider == "massive" and s.massive_api_key == "pk"


def test_polygon_aliases(clean_env):
    clean_env.setenv("US_PRICE_PROVIDER", "polygon")
    clean_env.setenv("POLYGON_API_KEY", "pk")
    clean_env.setenv("POLYGON_BASE_URL", "https://api.polygon.io")
    s = make()
    assert s.us_price_provider == "massive" and s.massive_api_key == "pk" and s.massive_base_url == "https://api.polygon.io"


def test_invalid_values_are_rejected(clean_env):
    clean_env.setenv("US_PRICE_PROVIDER", "yahoo")
    with pytest.raises(ValueError):
        make()
    clean_env.setenv("US_PRICE_PROVIDER", "")
    clean_env.setenv("JQUANTS_PLAN", "gold")
    with pytest.raises(ValueError):
        make()


def test_sec_user_agent_without_contact_is_provisional(clean_env):
    assert make().sec_ua_is_provisional is True
    clean_env.setenv("SEC_USER_AGENT", "AI-STOCK-INTELLIGENCE")
    assert make().sec_ua_is_provisional is True


def test_env_file_in_project_root_is_read(clean_env, tmp_path):
    f = tmp_path / ".env"
    f.write_text("JQUANTS_API_KEY=from-file\nFRED_API_KEY=\n", encoding="utf-8")
    s = Settings(_env_file=(f,))
    assert s.jquants_api_key == "from-file" and s.fred_api_key == ""


def test_empty_values_in_env_file_fall_back_to_defaults(clean_env, tmp_path):
    f = tmp_path / ".env"
    f.write_text("DATABASE_URL=\nJQUANTS_PLAN=\nMASSIVE_BASE_URL=\nSEC_UNIVERSE_SIZE=\nSEC_EXTRA_TICKERS=\n", encoding="utf-8")
    s = Settings(_env_file=(f,))
    assert s.database_url.startswith("sqlite:///") and s.jquants_plan == "free"
    assert s.massive_base_url == "https://api.polygon.io" and s.sec_universe_size == 150 and s.sec_extra_tickers.startswith("KO")


def test_fred_api_json_parser():
    rows, missing = fred.parse_api({"observations": [{"date": "2026-11-02", "value": "3.88"}, {"date": "2026-11-03", "value": "."}]})
    assert len(rows) == 1 and rows[0][1] == 3.88 and missing == 1


def test_fred_invalid_key_falls_back_to_csv():
    class Resp:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

        def raise_for_status(self):
            pass

    class FakeHttp:
        def __init__(self):
            self.urls = []

        def get(self, url, params=None):
            self.urls.append(url)
            if "api.stlouisfed.org" in url:
                return Resp(400, '{"error_message":"The value for variable api_key is not registered."}')
            return Resp(200, "observation_date,DFF\n2026-11-02,3.88\n")

    c = fred.FredClient("bad-key")
    c.http = FakeHttp()
    rows, _ = c.fetch("DFF")
    assert rows[0][1] == 3.88 and c.mode == "csv" and c.key_rejected and "fredgraph.csv" in c.http.urls[-1]


def test_secrets_are_redacted_from_errors():
    import httpx
    from app.providers.http import HttpError, check, redact
    assert redact("https://x/obs?series_id=DFF&api_key=SECRET123&file_type=json") == "https://x/obs?series_id=DFF&api_key=***&file_type=json"
    assert "SECRET" not in redact("...apiKey=SECRET&x=1 token=SECRET")
    r = httpx.Response(403, request=httpx.Request("GET", "https://api.example/x?apiKey=SECRET"))
    with pytest.raises(HttpError) as e:
        check(r)
    assert "SECRET" not in str(e.value) and "403" in str(e.value)
