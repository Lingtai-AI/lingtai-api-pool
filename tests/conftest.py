import textwrap
from pathlib import Path

import pytest

from lingtai_api_pool.config import parse_config


def make_config_data(**pool_overrides):
    data = {
        "pool": {
            "retry_statuses": [429, 500, 502, 503, 504],
            "max_attempts": 3,
            "cooldown_seconds": 30.0,
            **pool_overrides,
        },
        "upstream": [
            {
                "id": "primary",
                "base_url": "https://api.primary.test",
                "weight": 2,
                "headers": {"x-client": "lingtai"},
                "header_env": {"authorization": "PRIMARY_AUTH"},
            },
            {
                "id": "secondary",
                "base_url": "https://api.secondary.test",
                "weight": 1,
                "header_env": {"authorization": "SECONDARY_AUTH"},
            },
        ],
    }
    return data


@pytest.fixture
def app_config():
    return parse_config(make_config_data())


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    content = textwrap.dedent(
        """
        [pool]
        retry_statuses = [429, 503]
        max_attempts = 2
        cooldown_seconds = 5.0

        [[upstream]]
        id = "primary"
        base_url = "https://api.primary.test"
        weight = 2
        headers = { "x-client" = "lingtai" }
        header_env = { "authorization" = "PRIMARY_AUTH" }

        [[upstream]]
        id = "secondary"
        base_url = "https://api.secondary.test"
        """
    )
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path
