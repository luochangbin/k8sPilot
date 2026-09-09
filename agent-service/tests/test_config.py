"""Configuration loading tests."""

import runpy
from pathlib import Path
from unittest.mock import patch


def test_loads_dotenv_from_agent_service_root() -> None:
    config_file = Path(__file__).resolve().parents[1] / "app" / "config.py"
    expected_env = Path(__file__).resolve().parents[1] / ".env"

    with patch("dotenv.load_dotenv") as load_dotenv:
        namespace = runpy.run_path(str(config_file))

    load_dotenv.assert_called_once_with(expected_env)
    assert namespace["_AGENT_SERVICE_ROOT"] == expected_env.parent
