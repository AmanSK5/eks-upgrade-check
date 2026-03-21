"""Helm chart staleness and compatibility checks."""

import json
import subprocess
from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


# known chart compatibility thresholds — best-effort, not exhaustive
# if a chart isn't here, it just won't be checked
KNOWN_CHART_COMPAT = {
    "consul": {
        "min_recommended": "1.3.0",
        "notes": "Consul <1.3.0 may have issues with K8s 1.29+. Check HashiCorp compatibility matrix.",
    },
    "vault": {
        "min_recommended": "0.27.0",
        "notes": "Vault Helm <0.27.0 may use deprecated APIs. Check HashiCorp compatibility matrix.",
    },
    "gitlab": {
        "min_recommended": "7.7.0",
        "notes": "GitLab chart has strict K8s version requirements. Check GitLab support matrix.",
    },
    "kube-prometheus-stack": {
        "min_recommended": "55.0.0",
        "notes": "Older versions may use deprecated APIs removed in K8s 1.25+.",
    },
    "ingress-nginx": {
        "min_recommended": "4.8.0",
        "notes": "Older versions may not support newer K8s API changes.",
    },
    "cert-manager": {
        "min_recommended": "1.13.0",
        "notes": "cert-manager versions should align with K8s support matrix.",
    },
    "external-dns": {
        "min_recommended": "1.14.0",
        "notes": "Older versions may use deprecated API versions.",
    },
    "argocd": {
        "min_recommended": "5.50.0",
        "notes": "Argo CD chart compatibility varies with K8s version.",
    },
}

# Charts that are deprecated or replaced
DEPRECATED_CHARTS = {
    "aad-pod-identity": {
        "severity": "WARNING",
        "message": "aad-pod-identity is deprecated by Microsoft and not needed on EKS",
        "recommendation": "Remove aad-pod-identity — use IRSA or EKS Pod Identity for pod-level IAM on EKS.",
    },
    "stable/nginx-ingress": {
        "severity": "WARNING",
        "message": "stable/nginx-ingress is from the deprecated Helm stable repo",
        "recommendation": "Migrate to ingress-nginx from the kubernetes/ingress-nginx repo.",
    },
}


class HelmCheck(BaseCheck):
    """Check Helm releases for staleness and K8s compatibility."""

    title = "Helm Releases"

    def run(self, context: dict) -> list[CheckResult]:
        results = []
        target = context["target_version"]

        # Get Helm releases
        releases = self._get_helm_releases()

        if releases is None:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message="Helm is not installed or 'helm list' failed",
                detail="Skipping Helm chart checks. Install Helm to enable this check.",
            ))
            return results

        if not releases:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message="No Helm releases found in the cluster",
            ))
            return results

        results.append(CheckResult(
            severity="INFO",
            check=self.title,
            message=f"Found {len(releases)} Helm release(s) across all namespaces",
        ))

        failed_releases = []
        outdated_releases = []
        deprecated_found = []

        for release in releases:
            name = release.get("name", "unknown")
            namespace = release.get("namespace", "default")
            chart = release.get("chart", "")
            status = release.get("status", "")
            app_version = release.get("app_version", "")
            revision = release.get("revision", "")
            updated = release.get("updated", "")

            # Check release status
            if status != "deployed":
                failed_releases.append(f"{namespace}/{name} (status: {status})")

            # Parse chart name and version
            chart_name, chart_version = self._parse_chart_string(chart)

            # Check against known compatibility data
            for known_name, compat in KNOWN_CHART_COMPAT.items():
                if chart_name.lower() == known_name:
                    if chart_version and self._version_lt(chart_version, compat["min_recommended"]):
                        outdated_releases.append({
                            "name": name,
                            "namespace": namespace,
                            "chart_name": chart_name,
                            "current_version": chart_version,
                            "min_recommended": compat["min_recommended"],
                            "notes": compat["notes"],
                        })

            # Check for deprecated charts
            for dep_pattern, dep_info in DEPRECATED_CHARTS.items():
                if chart_name.lower() == dep_pattern or name.lower() == dep_pattern:
                    deprecated_found.append({
                        "name": name,
                        "namespace": namespace,
                        "chart": chart,
                        **dep_info,
                    })

        # Report failed releases
        if failed_releases:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"{len(failed_releases)} Helm release(s) in non-deployed state",
                detail="\n".join(failed_releases[:10]),
                recommendation="Fix failed Helm releases before upgrading.",
            ))

        # Report outdated releases
        for rel in outdated_releases:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=(
                    f"'{rel['name']}' chart version {rel['current_version']} "
                    f"is below recommended {rel['min_recommended']}"
                ),
                detail=rel["notes"],
                resource=f"{rel['namespace']}/{rel['name']}",
                recommendation=(
                    f"Upgrade Helm release '{rel['name']}' ({rel['chart_name']}) "
                    f"to at least version {rel['min_recommended']}."
                ),
                fix=f"helm upgrade {rel['name']} {rel['chart_name']} --version {rel['min_recommended']} -n {rel['namespace']} --reuse-values",
            ))

        # Report deprecated charts
        for dep in deprecated_found:
            results.append(CheckResult(
                severity=dep["severity"],
                check=self.title,
                message=f"'{dep['name']}' ({dep['chart']}): {dep['message']}",
                recommendation=dep["recommendation"],
            ))

        # All good if nothing flagged
        if not failed_releases and not outdated_releases and not deprecated_found:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message="All Helm releases appear healthy and up to date",
            ))

        return results

    def _get_helm_releases(self):
        """Get all Helm releases via helm list."""
        try:
            result = subprocess.run(
                [
                    "helm", "list",
                    "--all-namespaces",
                    "--output", "json",
                ],
                capture_output=True, text=True, check=True,
            )
            return json.loads(result.stdout)
        except FileNotFoundError:
            return None
        except subprocess.CalledProcessError:
            return None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _parse_chart_string(chart: str) -> tuple:
        """Parse 'chart-name-1.2.3' into ('chart-name', '1.2.3')."""
        # Helm chart strings look like: consul-1.2.1, kube-prometheus-stack-51.0.3
        # Find the last '-' followed by a digit to split name from version
        parts = chart.rsplit("-", 1)
        if len(parts) == 2 and parts[1] and parts[1][0].isdigit():
            return parts[0], parts[1]

        # Try more aggressive splitting for charts like 'my-long-chart-name-1.2.3'
        import re
        match = re.match(r"^(.+?)-(\d+\.\d+.*)$", chart)
        if match:
            return match.group(1), match.group(2)

        return chart, ""

    @staticmethod
    def _version_lt(v1: str, v2: str) -> bool:
        """Simple semver less-than comparison."""
        try:
            def parse(v):
                return [int(x) for x in v.split(".")[:3]]
            return parse(v1) < parse(v2)
        except (ValueError, IndexError):
            return False
