"""Checks the upgrade path is valid and flags extended support deadlines."""

from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


# these dates are approximate — AWS doesn't expose them via API
EXTENDED_SUPPORT_END = {
    "1.24": "2025-01-31",
    "1.25": "2025-05-01",
    "1.26": "2025-07-01",
    "1.27": "2025-09-01",
    "1.28": "2025-11-01",
    "1.29": "2026-03-01",
    "1.30": "2026-07-01",
    "1.31": "2026-11-01",
}


class ControlPlaneCheck(BaseCheck):
    """Validates upgrade path, catches multi-hop requirements, checks support dates."""

    title = "Control Plane Upgrade Path"

    def run(self, context: dict) -> list[CheckResult]:
        results = []
        current = context["current_version"]
        target = context["target_version"]

        current_parts = self._parse_version(current)
        target_parts = self._parse_version(target)

        if not current_parts or not target_parts:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"Cannot parse versions: current={current}, target={target}",
            ))
            return results

        # Check upgrade direction
        if target_parts <= current_parts:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"Target version {target} is not higher than current {current}",
                recommendation=f"Specify a target version higher than {current}.",
            ))
            return results

        # Calculate upgrade hops
        minor_diff = target_parts[1] - current_parts[1]

        if minor_diff == 1:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message=f"Control plane upgrade path valid ({current} → {target})",
            ))
        elif minor_diff > 1:
            # Build upgrade path
            path = []
            for v in range(current_parts[1] + 1, target_parts[1] + 1):
                path.append(f"1.{v}")
            path_str = " → ".join([current] + path)

            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Multi-hop upgrade required: {minor_diff} minor version(s)",
                detail=f"Upgrade path: {path_str}\nEKS does not support skipping minor versions.",
                recommendation=f"Plan sequential upgrades: {path_str}",
            ))

        # Check extended support / EOL
        if current in EXTENDED_SUPPORT_END:
            end_date = EXTENDED_SUPPORT_END[current]
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Version {current} extended support ends {end_date}",
                detail="Extended support incurs additional per-cluster-hour charges.",
                recommendation=f"Upgrade from {current} before {end_date} to avoid extended support costs.",
            ))

        # Check cluster health
        try:
            cluster_info = context["cluster_info"]
            status = cluster_info["cluster"]["status"]
            if status == "ACTIVE":
                results.append(CheckResult(
                    severity="PASS",
                    check=self.title,
                    message=f"Cluster status is ACTIVE",
                ))
            else:
                results.append(CheckResult(
                    severity="ERROR",
                    check=self.title,
                    message=f"Cluster status is {status} (expected ACTIVE)",
                    recommendation="Resolve cluster issues before attempting upgrade.",
                ))
        except Exception:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message="Could not verify cluster status",
            ))

        # Check platform version
        try:
            platform_version = cluster_info["cluster"].get("platformVersion", "unknown")
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message=f"Platform version: {platform_version}",
            ))
        except Exception:
            pass

        return results

    @staticmethod
    def _parse_version(v: str) -> tuple:
        """Parse '1.29' into (1, 29)."""
        try:
            parts = v.split(".")
            return (int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return None
