"""Tests for questdb_init's connection retry."""
import pytest

from scripts import questdb_init
from scripts.questdb_init import ATTEMPT_TIMEOUT, connect_with_retry


class FakePsycopg:
    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    def connect(self, dsn, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise OSError('connection timeout expired')
        return 'conn'


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(questdb_init.time, 'sleep', lambda s: None)


def test_every_attempt_is_bounded():
    """An unbounded attempt can outlast the whole wait, leaving one try."""
    fake = FakePsycopg(failures=0)
    connect_with_retry(fake, 'dsn', timeout=60)
    assert fake.calls[0]['connect_timeout'] == ATTEMPT_TIMEOUT
    assert ATTEMPT_TIMEOUT < 60


def test_a_failed_attempt_is_retried():
    fake = FakePsycopg(failures=2)
    assert connect_with_retry(fake, 'dsn', timeout=60) == 'conn'
    assert len(fake.calls) == 3


def test_it_gives_up_after_the_timeout():
    fake = FakePsycopg(failures=10**6)
    assert connect_with_retry(fake, 'dsn', timeout=0) is None
    assert len(fake.calls) == 1
