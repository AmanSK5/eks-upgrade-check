"""Scans running workloads for K8s APIs that get removed in the target version.

Complements EKS Insights — Insights checks audit logs (last 30 days of API calls),
this scans what's actually deployed right now. Catches dormant resources that
Insights would miss.
"""

import json
from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


# known API removals per K8s version — needs manual updates as new versions land
# format: version -> list of (old_api_version, kind, removed_in, replacement)
DEPRECATED_APIS = {
    "1.25": [
        ("batch/v1beta1", "CronJob", "1.25", "batch/v1"),
        ("discovery.k8s.io/v1beta1", "EndpointSlice", "1.25", "discovery.k8s.io/v1"),
        ("events.k8s.io/v1beta1", "Event", "1.25", "events.k8s.io/v1"),
        ("autoscaling/v2beta1", "HorizontalPodAutoscaler", "1.25", "autoscaling/v2"),
        ("policy/v1beta1", "PodDisruptionBudget", "1.25", "policy/v1"),
        ("policy/v1beta1", "PodSecurityPolicy", "1.25", "Removed entirely — use Pod Security Admission"),
        ("node.k8s.io/v1beta1", "RuntimeClass", "1.25", "node.k8s.io/v1"),
    ],
    "1.26": [
        ("flowcontrol.apiserver.k8s.io/v1beta1", "FlowSchema", "1.26", "flowcontrol.apiserver.k8s.io/v1beta3"),
        ("flowcontrol.apiserver.k8s.io/v1beta1", "PriorityLevelConfiguration", "1.26", "flowcontrol.apiserver.k8s.io/v1beta3"),
        ("autoscaling/v2beta2", "HorizontalPodAutoscaler", "1.26", "autoscaling/v2"),
    ],
    "1.27": [
        ("storage.k8s.io/v1beta1", "CSIStorageCapacity", "1.27", "storage.k8s.io/v1"),
    ],
    "1.29": [
        ("flowcontrol.apiserver.k8s.io/v1beta2", "FlowSchema", "1.29", "flowcontrol.apiserver.k8s.io/v1"),
        ("flowcontrol.apiserver.k8s.io/v1beta2", "PriorityLevelConfiguration", "1.29", "flowcontrol.apiserver.k8s.io/v1"),
    ],
    "1.32": [
        ("flowcontrol.apiserver.k8s.io/v1beta3", "FlowSchema", "1.32", "flowcontrol.apiserver.k8s.io/v1"),
        ("flowcontrol.apiserver.k8s.io/v1beta3", "PriorityLevelConfiguration", "1.32", "flowcontrol.apiserver.k8s.io/v1"),
    ],
}


class DeprecatedAPICheck(BaseCheck):
    """Scan workloads for deprecated or removed API versions."""

    title = "Deprecated / Removed APIs"

    def run(self, context: dict) -> list[CheckResult]:
        results = []
        current = context["current_version"]
        target = context["target_version"]

        # Build a lookup of all APIs that will be removed between current+1 and target (inclusive)
        current_minor = int(current.split(".")[1])
        target_minor = int(target.split(".")[1])

        removals = {}  # (api_version, kind) -> (removed_in, replacement)
        for version_str, apis in DEPRECATED_APIS.items():
            v_minor = int(version_str.split(".")[1])
            if current_minor < v_minor <= target_minor + 2:
                # Include removals in target and the next version (to warn ahead)
                for api_version, kind, removed_in, replacement in apis:
                    removals[(api_version, kind)] = (removed_in, replacement)

        if not removals:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message="No known API removals between current and target versions",
            ))
            return results

        # Scan cluster resources for deprecated API usage
        found_deprecated = []

        try:
            raw = self.k8s.get_all_resources_raw()
            resources = json.loads(raw)
            items = resources.get("items", [])

            for item in items:
                api_version = item.get("apiVersion", "")
                kind = item.get("kind", "")
                name = item.get("metadata", {}).get("name", "unknown")
                namespace = item.get("metadata", {}).get("namespace", "default")

                key = (api_version, kind)
                if key in removals:
                    removed_in, replacement = removals[key]
                    found_deprecated.append({
                        "api_version": api_version,
                        "kind": kind,
                        "name": name,
                        "namespace": namespace,
                        "removed_in": removed_in,
                        "replacement": replacement,
                    })
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not scan all resources: {e}",
                detail="Some deprecated API usage may be missed.",
            ))

        # Also check using kubectl for Helm-managed resources
        try:
            helm_output = self.k8s.run_kubectl([
                "get", "all,ingress,networkpolicy,pdb,hpa",
                "--all-namespaces", "-o", "json",
            ])
            helm_resources = json.loads(helm_output)
            for item in helm_resources.get("items", []):
                api_version = item.get("apiVersion", "")
                kind = item.get("kind", "")
                name = item.get("metadata", {}).get("name", "unknown")
                namespace = item.get("metadata", {}).get("namespace", "default")

                key = (api_version, kind)
                if key in removals:
                    # Deduplicate
                    already_found = any(
                        d["name"] == name and d["namespace"] == namespace
                        for d in found_deprecated
                    )
                    if not already_found:
                        removed_in, replacement = removals[key]
                        found_deprecated.append({
                            "api_version": api_version,
                            "kind": kind,
                            "name": name,
                            "namespace": namespace,
                            "removed_in": removed_in,
                            "replacement": replacement,
                        })
        except Exception:
            pass

        # Report findings
        if not found_deprecated:
            results.append(CheckResult(
                severity="PASS",
                check=self.title,
                message="No deprecated API usage detected in running workloads",
            ))
        else:
            # Group by removal version
            by_version = {}
            for d in found_deprecated:
                by_version.setdefault(d["removed_in"], []).append(d)

            for removed_in, items in sorted(by_version.items()):
                removed_minor = int(removed_in.split(".")[1])
                target_minor = int(target.split(".")[1])

                # ERROR if removed in target version or before, WARNING if removed after
                if removed_minor <= target_minor:
                    severity = "ERROR"
                else:
                    severity = "WARNING"

                detail_lines = []
                for item in items:
                    detail_lines.append(
                        f"{item['namespace']}/{item['name']} "
                        f"({item['kind']} using {item['api_version']}) "
                        f"→ migrate to {item['replacement']}"
                    )

                results.append(CheckResult(
                    severity=severity,
                    check=self.title,
                    message=f"Deprecated APIs detected (removed in {removed_in}): {len(items)} resource(s)",
                    detail="\n".join(detail_lines[:10]) + (
                        f"\n... and {len(detail_lines) - 10} more" if len(detail_lines) > 10 else ""
                    ),
                    recommendation=f"Update {len(items)} resource(s) using APIs removed in {removed_in}.",
                ))

        return results
