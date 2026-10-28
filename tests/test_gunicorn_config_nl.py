from gunicorn_config import max_requests, max_requests_jitter, timeout, workers


def test_gunicorn_config():
    assert max_requests == 1000
    assert max_requests_jitter == 100
    assert timeout == 30
    assert workers == 5
