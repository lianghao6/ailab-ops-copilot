"""CLI contracts for the explicitly unsupported V1 compatibility namespace."""

import httpx

from ailab_ops import bench, cli


def install_discovery_transport(monkeypatch, handler):
    client_class = httpx.Client

    def client(**kwargs):
        assert kwargs["trust_env"] is False, "loopback discovery must bypass proxy settings"
        return client_class(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)


def test_legacy_bench_without_jobs_discovers_only_failed_jobs(monkeypatch, capsys):
    """Catch a broken discovery import or forwarding successful jobs to the load runner."""
    def discover(request):
        assert request.method == "GET"
        assert str(request.url) == "http://127.0.0.1:8081/v1/jobs?limit=40"
        return httpx.Response(200, json={"jobs": [
            {"job_id": "job-succeeded", "failed": False},
            {"job_id": "job-failed-one", "failed": True},
            {"job_id": "job-failed-two", "failed": True},
        ]})

    install_discovery_transport(monkeypatch, discover)
    received = []

    async def run_benchmark(**kwargs):
        received.append(kwargs)
        return []  # Exercise CLI discovery/dispatch without running a long load test.

    monkeypatch.setattr(bench, "run_benchmark", run_benchmark)
    assert cli.main(["legacy", "bench", "--base-url", "http://127.0.0.1:8081",
                     "--scenario", "steady", "--concurrency", "1", "--requests", "1"]) == 0
    assert received == [{"base_url": "http://127.0.0.1:8081", "scenarios": ["steady"],
                         "concurrency": 1, "n_requests": 1,
                         "job_ids": ["job-failed-one", "job-failed-two"], "rate_per_client": None}]
    assert "job pool    2" in capsys.readouterr().out


def test_legacy_bench_without_failed_jobs_stops_before_load(monkeypatch, capsys):
    install_discovery_transport(monkeypatch,
        lambda request: httpx.Response(200, json={"jobs": [{"job_id": "healthy", "failed": False}]}))
    assert cli.main(["legacy", "bench"]) == 2
    assert "no failed jobs" in capsys.readouterr().err


def test_legacy_bench_discovery_failure_points_to_legacy_server(monkeypatch, capsys):
    def unavailable(request):
        raise httpx.ConnectError("controlled discovery failure", request=request)

    install_discovery_transport(monkeypatch, unavailable)
    assert cli.main(["legacy", "bench"]) == 2
    error = capsys.readouterr().err
    assert "cannot reach" in error and "make legacy-serve" in error
