"""Checks EKS add-ons — managed vs self-managed, version lag, target compatibility."""

import re
from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


# the three add-ons every EKS cluster has
CORE_ADDONS = {
    "kube-proxy": {
        "type": "daemonset",
        "namespace": "kube-system",
        "name": "kube-proxy",
    },
    "coredns": {
        "type": "deployment",
        "namespace": "kube-system",
        "name": "coredns",
    },
    "vpc-cni": {
        "type": "daemonset",
        "namespace": "kube-system",
        "name": "aws-node",
    },
}

# other common add-ons — only checked if they actually exist in the cluster
OPTIONAL_ADDONS = {
    "aws-ebs-csi-driver": {
        "type": "deployment",
        "namespace": "kube-system",
        "name": "ebs-csi-controller",
    },
    "aws-efs-csi-driver": {
        "type": "deployment",
        "namespace": "kube-system",
        "name": "efs-csi-controller",
    },
    "aws-mountpoint-s3-csi-driver": {
        "type": "daemonset",
        "namespace": "kube-system",
        "name": "s3-csi-node",
    },
    "snapshot-controller": {
        "type": "deployment",
        "namespace": "kube-system",
        "name": "snapshot-controller",
    },
    "adot": {
        "type": "deployment",
        "namespace": "opentelemetry-operator-system",
        "name": "opentelemetry-operator",
    },
    "amazon-cloudwatch-observability": {
        "type": "deployment",
        "namespace": "amazon-cloudwatch",
        "name": "cloudwatch-agent-operator",
    },
}


class AddonCheck(BaseCheck):
    """Check EKS add-on versions, managed vs self-managed, and compatibility."""

    title = "EKS Add-ons"

    def run(self, context: dict) -> list[CheckResult]:
        results = []
        target = context["target_version"]
        current = context["current_version"]

        # Get managed add-ons from EKS API
        try:
            managed_addons = self.aws.list_addons()
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not list managed add-ons: {e}",
            ))
            managed_addons = []

        managed_addon_info = {}
        for addon_name in managed_addons:
            try:
                info = self.aws.describe_addon(addon_name)
                addon = info.get("addon", {})
                managed_addon_info[addon_name] = {
                    "version": addon.get("addonVersion", "unknown"),
                    "status": addon.get("status", "unknown"),
                    "health": addon.get("health", {}),
                }
            except Exception:
                managed_addon_info[addon_name] = {"version": "unknown", "status": "unknown"}

        # Check core add-ons
        for addon_name, addon_meta in CORE_ADDONS.items():
            is_managed = addon_name in managed_addon_info

            if is_managed:
                info = managed_addon_info[addon_name]
                results.append(CheckResult(
                    severity="PASS",
                    check=self.title,
                    message=f"{addon_name} is EKS-managed ({info['version']}, status: {info['status']})",
                ))

                # Check if there's a compatible version for target
                self._check_target_compatibility(
                    results, addon_name, info["version"], target
                )
            else:
                # Self-managed — detect version from running workload
                version = self._detect_self_managed_version(addon_meta)

                results.append(CheckResult(
                    severity="WARNING",
                    check=self.title,
                    message=f"{addon_name} is SELF-MANAGED (detected: {version})",
                    detail="Self-managed add-ons must be manually upgraded and are not "
                           "automatically updated during EKS control plane upgrades.",
                    recommendation=f"Convert {addon_name} to an EKS-managed add-on before upgrading.",
                    fix=f"aws eks create-addon --cluster-name {context['cluster_name']} --addon-name {addon_name} --resolve-conflicts OVERWRITE",
                ))

                # Check version lag
                if version != "unknown":
                    self._check_version_lag(results, addon_name, version, current)

        # Check optional add-ons (only if they exist in the cluster)
        for addon_name, addon_meta in OPTIONAL_ADDONS.items():
            exists = self._workload_exists(addon_meta)
            if not exists:
                continue

            is_managed = addon_name in managed_addon_info
            if is_managed:
                info = managed_addon_info[addon_name]
                results.append(CheckResult(
                    severity="PASS",
                    check=self.title,
                    message=f"{addon_name} is EKS-managed ({info['version']})",
                ))
                self._check_target_compatibility(
                    results, addon_name, info["version"], target
                )
            else:
                version = self._detect_self_managed_version(addon_meta)
                results.append(CheckResult(
                    severity="WARNING",
                    check=self.title,
                    message=f"{addon_name} detected but NOT EKS-managed (version: {version})",
                    recommendation=f"Consider converting {addon_name} to an EKS-managed add-on.",
                ))

        # Report any managed add-ons with degraded health
        for addon_name, info in managed_addon_info.items():
            if info.get("status") not in ("ACTIVE", "unknown"):
                results.append(CheckResult(
                    severity="ERROR",
                    check=self.title,
                    message=f"{addon_name} status is {info['status']}",
                    recommendation=f"Resolve {addon_name} health issues before upgrading.",
                ))

        return results

    def _check_target_compatibility(self, results, addon_name, current_version, target_k8s):
        """Check if there's a compatible addon version for the target K8s version."""
        try:
            compat = self.aws.describe_addon_versions(addon_name, target_k8s)
            versions = compat.get("addons", [{}])[0].get("addonVersions", [])
            if versions:
                latest = versions[0].get("addonVersion", "unknown")
                results.append(CheckResult(
                    severity="INFO",
                    check=self.title,
                    message=f"{addon_name} latest compatible version for {target_k8s}: {latest}",
                ))
            else:
                results.append(CheckResult(
                    severity="WARNING",
                    check=self.title,
                    message=f"No compatible {addon_name} version found for K8s {target_k8s}",
                ))
        except Exception:
            pass

    def _check_version_lag(self, results, addon_name, detected_version, current_k8s):
        """Check if a self-managed add-on version is significantly behind."""
        # Extract the K8s minor version from the add-on version string if possible
        match = re.search(r"v?1\.(\d+)", detected_version)
        if match:
            addon_minor = int(match.group(1))
            cluster_minor = int(current_k8s.split(".")[1])
            lag = cluster_minor - addon_minor
            if lag >= 2:
                results.append(CheckResult(
                    severity="ERROR",
                    check=self.title,
                    message=f"{addon_name} is {lag} minor version(s) behind the cluster",
                    detail=f"Add-on version: {detected_version}, Cluster: {current_k8s}",
                    recommendation=f"Upgrade {addon_name} add-on to a {current_k8s}-compatible version.",
                ))
            elif lag == 1:
                results.append(CheckResult(
                    severity="WARNING",
                    check=self.title,
                    message=f"{addon_name} is 1 minor version behind the cluster",
                    detail=f"Add-on version: {detected_version}, Cluster: {current_k8s}",
                    recommendation=f"Upgrade {addon_name} add-on before upgrading the control plane.",
                ))

    def _detect_self_managed_version(self, addon_meta: dict) -> str:
        """Try to detect the version of a self-managed add-on from its container image."""
        try:
            if addon_meta["type"] == "daemonset":
                ds_list = self.k8s.get_daemonsets(namespace=addon_meta["namespace"])
                for ds in ds_list:
                    if ds.metadata.name == addon_meta["name"]:
                        containers = ds.spec.template.spec.containers
                        if containers:
                            image = containers[0].image
                            return self._extract_version_from_image(image)
            elif addon_meta["type"] == "deployment":
                dep_list = self.k8s.get_deployments(namespace=addon_meta["namespace"])
                for dep in dep_list:
                    if dep.metadata.name == addon_meta["name"]:
                        containers = dep.spec.template.spec.containers
                        if containers:
                            image = containers[0].image
                            return self._extract_version_from_image(image)
        except Exception:
            pass
        return "unknown"

    def _workload_exists(self, addon_meta: dict) -> bool:
        """Check if a workload exists in the cluster."""
        try:
            if addon_meta["type"] == "daemonset":
                ds_list = self.k8s.get_daemonsets(namespace=addon_meta["namespace"])
                return any(ds.metadata.name == addon_meta["name"] for ds in ds_list)
            elif addon_meta["type"] == "deployment":
                dep_list = self.k8s.get_deployments(namespace=addon_meta["namespace"])
                return any(dep.metadata.name == addon_meta["name"] for dep in dep_list)
        except Exception:
            pass
        return False

    @staticmethod
    def _extract_version_from_image(image: str) -> str:
        """Extract version tag from a container image string."""
        if ":" in image:
            tag = image.split(":")[-1]
            # Clean up EKS build suffixes for readability
            return tag
        return "latest"
