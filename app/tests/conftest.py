import pytest

from lapcounter.config import Config
from lapcounter.web import create_app
from lapcounter.web.auth import reset_throttle


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=str(tmp_path / "lc.db"), decoder_url="tcp://127.0.0.1:1")


@pytest.fixture
def app(cfg):
    reset_throttle()
    return create_app(cfg, testing=True)


@pytest.fixture
def client(app):
    return app.test_client()


def csrf_of(client):
    with client.session_transaction() as s:
        return s.get("csrf")


def get_csrf(client, path):
    client.get(path)
    return csrf_of(client)


def do_setup(client, username="riley", password="correct horse battery"):
    token = get_csrf(client, "/setup")
    return client.post("/setup", data={"csrf": token, "username": username,
                                       "password": password, "confirm": password})
