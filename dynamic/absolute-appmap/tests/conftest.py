from pathlib import Path

import pytest

from appmap.config import load_config

DATA = Path(__file__).parent / "data"


@pytest.fixture
def cfg():
    return load_config(DATA / "config_staging.yaml")


@pytest.fixture
def prod_cfg():
    return load_config(DATA / "config_prod.yaml")
