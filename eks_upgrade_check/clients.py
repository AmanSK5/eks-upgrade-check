"""Wrappers around boto3 and the k8s Python client."""

import subprocess
import json
import os
from dataclasses import dataclass
from typing import Optional

import boto3
from kubernetes import client as k8s_client, config as k8s_config


class AWSClient:
    """Handles all the AWS API calls we need — EKS, EC2, IAM, SSM."""

    def __init__(
        self,
        cluster_name: str,
        region: Optional[str] = None,
        profile: Optional[str] = None,
    ):
        self.cluster_name = cluster_name
        session_kwargs = {}
        if profile:
            session_kwargs["profile_name"] = profile
        if region:
            session_kwargs["region_name"] = region

        self.session = boto3.Session(**session_kwargs)
        self.region = self.session.region_name or "us-east-1"

        self.eks = self.session.client("eks", region_name=self.region)
        self.ec2 = self.session.client("ec2", region_name=self.region)
        self.iam = self.session.client("iam", region_name=self.region)
        self.elbv2 = self.session.client("elbv2", region_name=self.region)
        self.ssm = self.session.client("ssm", region_name=self.region)

    def get_supported_versions(self) -> list:
        """Pull the list of K8s versions EKS currently supports.
        
        Bit of a hack — we query kube-proxy addon versions since it exists
        on every cluster, and extract the compatible K8s versions from there.
        """
        versions = set()
        try:
            paginator = self.eks.get_paginator("describe_addon_versions")
            for page in paginator.paginate(addonName="kube-proxy"):
                for addon in page.get("addons", []):
                    for av in addon.get("addonVersions", []):
                        for compat in av.get("compatibilities", []):
                            cv = compat.get("clusterVersion", "")
                            if cv:
                                versions.add(cv)
        except Exception:
            # Fallback without paginator
            try:
                response = self.eks.describe_addon_versions(addonName="kube-proxy")
                for addon in response.get("addons", []):
                    for av in addon.get("addonVersions", []):
                        for compat in av.get("compatibilities", []):
                            cv = compat.get("clusterVersion", "")
                            if cv:
                                versions.add(cv)
            except Exception:
                pass

        return sorted(versions, key=lambda v: int(v.split(".")[1]))

    def describe_cluster(self) -> dict:
        """Describe the EKS cluster."""
        return self.eks.describe_cluster(name=self.cluster_name)

    def list_addons(self) -> list:
        """List EKS managed add-ons."""
        response = self.eks.list_addons(clusterName=self.cluster_name)
        return response.get("addons", [])

    def describe_addon(self, addon_name: str) -> dict:
        """Describe a specific managed add-on."""
        return self.eks.describe_addon(
            clusterName=self.cluster_name,
            addonName=addon_name,
        )

    def describe_addon_versions(self, addon_name: str, k8s_version: str) -> dict:
        """Get available versions for an add-on at a given K8s version."""
        return self.eks.describe_addon_versions(
            addonName=addon_name,
            kubernetesVersion=k8s_version,
        )

    def list_nodegroups(self) -> list:
        """List managed node groups."""
        response = self.eks.list_nodegroups(clusterName=self.cluster_name)
        return response.get("nodegroups", [])

    def describe_nodegroup(self, nodegroup_name: str) -> dict:
        """Describe a managed node group."""
        return self.eks.describe_nodegroup(
            clusterName=self.cluster_name,
            nodegroupName=nodegroup_name,
        )

    def get_latest_ami(self, k8s_version: str, ami_type: str = "AL2023_x86_64_STANDARD") -> Optional[str]:
        """Get the latest EKS-optimised AMI for a given version and type."""
        ami_type_map = {
            "AL2023_x86_64_STANDARD": f"/aws/service/eks/optimized-ami/{k8s_version}/amazon-linux-2023/x86_64/standard/recommended/image_id",
            "AL2_x86_64": f"/aws/service/eks/optimized-ami/{k8s_version}/amazon-linux-2/recommended/image_id",
        }
        param_path = ami_type_map.get(ami_type)
        if not param_path:
            return None
        try:
            response = self.ssm.get_parameter(Name=param_path)
            return response["Parameter"]["Value"]
        except Exception:
            return None

    def list_insights(self) -> list:
        """List all EKS cluster insights (upgrade readiness + configuration)."""
        insights = []
        try:
            paginator = self.eks.get_paginator("list_insights")
            for page in paginator.paginate(clusterName=self.cluster_name):
                insights.extend(page.get("insights", []))
        except Exception:
            # Fallback for older boto3 without paginator support
            try:
                response = self.eks.list_insights(clusterName=self.cluster_name)
                insights = response.get("insights", [])
            except Exception:
                pass
        return insights

    def describe_insight(self, insight_id: str) -> dict:
        """Get detailed info for a specific insight."""
        return self.eks.describe_insight(
            clusterName=self.cluster_name,
            id=insight_id,
        )

    def get_launch_template(self, lt_id: str, version: str = "$Latest") -> Optional[dict]:
        """Describe a launch template version."""
        try:
            response = self.ec2.describe_launch_template_versions(
                LaunchTemplateId=lt_id,
                Versions=[version],
            )
            versions = response.get("LaunchTemplateVersions", [])
            return versions[0] if versions else None
        except Exception:
            return None


class K8sClient:
    """Wraps the k8s Python client. Expects kubeconfig to already be set up."""

    def __init__(self, cluster_name: str, aws_client: AWSClient):
        self.cluster_name = cluster_name
        self.aws_client = aws_client

        # expects ~/.kube/config to already point at the right cluster
        k8s_config.load_kube_config()

        self.core_v1 = k8s_client.CoreV1Api()
        self.apps_v1 = k8s_client.AppsV1Api()
        self.policy_v1 = k8s_client.PolicyV1Api()
        self.api_client = k8s_client.ApiClient()

    def get_nodes(self) -> list:
        """Get all nodes."""
        return self.core_v1.list_node().items

    def get_pods(self, namespace: str = None) -> list:
        """Get pods, optionally filtered by namespace."""
        if namespace:
            return self.core_v1.list_namespaced_pod(namespace).items
        return self.core_v1.list_pod_for_all_namespaces().items

    def get_deployments(self, namespace: str = None) -> list:
        """Get deployments, optionally filtered by namespace."""
        if namespace:
            return self.apps_v1.list_namespaced_deployment(namespace).items
        return self.apps_v1.list_deployment_for_all_namespaces().items

    def get_daemonsets(self, namespace: str = None) -> list:
        """Get daemonsets."""
        if namespace:
            return self.apps_v1.list_namespaced_daemon_set(namespace).items
        return self.apps_v1.list_daemon_set_for_all_namespaces().items

    def get_statefulsets(self, namespace: str = None) -> list:
        """Get statefulsets."""
        if namespace:
            return self.apps_v1.list_namespaced_stateful_set(namespace).items
        return self.apps_v1.list_stateful_set_for_all_namespaces().items

    def get_pdbs(self, namespace: str = None) -> list:
        """Get PodDisruptionBudgets."""
        if namespace:
            return self.policy_v1.list_namespaced_pod_disruption_budget(namespace).items
        return self.policy_v1.list_pod_disruption_budget_for_all_namespaces().items

    def get_service_accounts(self, namespace: str = None) -> list:
        """Get service accounts."""
        if namespace:
            return self.core_v1.list_namespaced_service_account(namespace).items
        return self.core_v1.list_service_account_for_all_namespaces().items

    def get_api_resources(self) -> list:
        """Get all available API resources by querying the discovery endpoint."""
        # Use kubectl for broader API resource discovery
        try:
            result = subprocess.run(
                ["kubectl", "api-resources", "-o", "wide", "--no-headers"],
                capture_output=True, text=True, check=True,
            )
            resources = []
            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 4:
                    resources.append({
                        "name": parts[0],
                        "apigroup": parts[2] if len(parts) > 4 else "",
                        "kind": parts[-2] if len(parts) > 4 else parts[-1],
                    })
            return resources
        except Exception:
            return []

    def get_all_resources_raw(self) -> str:
        """Get all resources with their API versions via kubectl."""
        try:
            result = subprocess.run(
                [
                    "kubectl", "get",
                    "deployments,statefulsets,daemonsets,jobs,cronjobs,"
                    "ingresses,networkpolicies,podsecuritypolicies,"
                    "horizontalpodautoscalers,poddisruptionbudgets,"
                    "services,configmaps,secrets",
                    "--all-namespaces",
                    "-o", "json",
                ],
                capture_output=True, text=True, check=True,
            )
            return result.stdout
        except Exception:
            return "{}"

    def run_kubectl(self, args: list) -> str:
        """Run an arbitrary kubectl command and return stdout."""
        try:
            result = subprocess.run(
                ["kubectl"] + args,
                capture_output=True, text=True, check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"kubectl failed: {e.stderr}") from e
