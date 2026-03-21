"""Base class for checks."""

from abc import ABC, abstractmethod
from eks_upgrade_check.reporter import CheckResult


class BaseCheck(ABC):
    """Base class for all checks."""

    title: str = "Unknown Check"

    def __init__(self, aws_client, k8s_client):
        self.aws = aws_client
        self.k8s = k8s_client

    @abstractmethod
    def run(self, context: dict) -> list[CheckResult]:
        """Run the check and return a list of CheckResults."""
        ...
