import pytest


class ResultCapturePlugin:
    def __init__(self):
        self.results = []

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.results.append({
                "nodeid": report.nodeid,
                "outcome": report.outcome,
                "longrepr": report.longrepr if report.failed else None,
            })
