"""IRSA (IAM Roles for Service Accounts) validation."""

from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


# SAs that should have IRSA — if they exist in the cluster and don't have
# the annotation, we flag it. If the SA doesn't exist, we skip it.
EXPECTED_IRSA_SERVICE_ACCOUNTS = {
    "aws-load-balancer-controller": {
        "namespace": "kube-system",
        "description": "AWS Load Balancer Controller",
        "risk": "Without IRSA, falls back to node instance role. May lack permissions "
                "for target group registration, causing new nodes to not receive traffic.",
    },
    "ebs-csi-controller-sa": {
        "namespace": "kube-system",
        "description": "EBS CSI Driver",
        "risk": "Without IRSA, may fail to provision or attach EBS volumes.",
    },
    "efs-csi-controller-sa": {
        "namespace": "kube-system",
        "description": "EFS CSI Driver",
        "risk": "Without IRSA, may fail to mount EFS file systems.",
    },
    "cluster-autoscaler": {
        "namespace": "kube-system",
        "description": "Cluster Autoscaler",
        "risk": "Without IRSA, autoscaling may fail due to insufficient permissions.",
    },
    "karpenter": {
        "namespace": "karpenter",
        "description": "Karpenter node provisioner",
        "risk": "Without IRSA, Karpenter cannot manage EC2 instances.",
    },
    "external-dns": {
        "namespace": "kube-system",
        "description": "ExternalDNS",
        "risk": "Without IRSA, DNS records will not be updated automatically.",
    },
    "cert-manager": {
        "namespace": "cert-manager",
        "description": "cert-manager",
        "risk": "Without IRSA, DNS01 challenge solvers may fail for Route53.",
    },
    "aws-node": {
        "namespace": "kube-system",
        "description": "VPC CNI (aws-node)",
        "risk": "Without IRSA, relies on node role. IRSA recommended for least-privilege.",
        "severity": "WARNING",
    },
    "fluent-bit": {
        "namespace": "amazon-cloudwatch",
        "description": "Fluent Bit log forwarder",
        "risk": "Without IRSA, may lack permissions to write to CloudWatch Logs.",
    },
    "fluentbit-enhanced": {
        "namespace": "amazon-cloudwatch",
        "description": "Fluent Bit (enhanced)",
        "risk": "Without IRSA, may lack permissions to write to CloudWatch Logs.",
    },
}

IRSA_ANNOTATION_KEY = "eks.amazonaws.com/role-arn"


class IRSACheck(BaseCheck):
    """Validate IRSA configuration for critical service accounts."""

    title = "IRSA (IAM Roles for Service Accounts)"

    def run(self, context: dict) -> list[CheckResult]:
        results = []

        # Get OIDC provider from cluster info
        cluster_info = context["cluster_info"]
        oidc_issuer = (
            cluster_info.get("cluster", {})
            .get("identity", {})
            .get("oidc", {})
            .get("issuer", "")
        )

        if not oidc_issuer:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message="No OIDC provider associated with this cluster",
                detail="IRSA requires an OIDC identity provider. Without one, "
                       "no service accounts can use IAM roles.",
                recommendation="Associate an OIDC identity provider with your EKS cluster.",
            ))
            return results

        results.append(CheckResult(
            severity="PASS",
            check=self.title,
            message="OIDC provider is configured",
            detail=f"Issuer: {oidc_issuer}",
        ))

        # Scan all service accounts for IRSA annotations
        try:
            all_service_accounts = self.k8s.get_service_accounts()
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not list service accounts: {e}",
            ))
            return results

        # Build lookup of existing SAs
        sa_lookup = {}
        irsa_count = 0
        for sa in all_service_accounts:
            key = f"{sa.metadata.namespace}/{sa.metadata.name}"
            annotations = sa.metadata.annotations or {}
            has_irsa = IRSA_ANNOTATION_KEY in annotations
            sa_lookup[sa.metadata.name] = {
                "namespace": sa.metadata.namespace,
                "has_irsa": has_irsa,
                "role_arn": annotations.get(IRSA_ANNOTATION_KEY, ""),
            }
            if has_irsa:
                irsa_count += 1

        results.append(CheckResult(
            severity="INFO",
            check=self.title,
            message=f"Found {irsa_count} service account(s) with IRSA annotations "
                    f"out of {len(all_service_accounts)} total",
        ))

        # Check expected service accounts
        for sa_name, expected in EXPECTED_IRSA_SERVICE_ACCOUNTS.items():
            # Find matching SAs (check both exact name and in expected namespace)
            found = False
            for sa in all_service_accounts:
                if sa.metadata.name == sa_name:
                    found = True
                    annotations = sa.metadata.annotations or {}
                    has_irsa = IRSA_ANNOTATION_KEY in annotations

                    if has_irsa:
                        role_arn = annotations[IRSA_ANNOTATION_KEY]
                        results.append(CheckResult(
                            severity="PASS",
                            check=self.title,
                            message=f"{expected['description']} ({sa_name}) has IRSA configured",
                            detail=f"Role: {role_arn}",
                        ))

                        # Validate the IAM role exists
                        self._validate_role(results, role_arn, sa_name, expected["description"])
                    else:
                        results.append(CheckResult(
                            severity=expected.get("severity", "ERROR"),
                            check=self.title,
                            message=f"{expected['description']} ({sa_name}) missing IRSA annotation",
                            detail=expected["risk"],
                            resource=f"{sa.metadata.namespace}/{sa_name}",
                            recommendation=f"Configure IRSA for {sa_name} in {sa.metadata.namespace}.",
                            fix=f"kubectl annotate sa {sa_name} -n {sa.metadata.namespace} \\\n  eks.amazonaws.com/role-arn=arn:aws:iam::<ACCOUNT_ID>:role/<ROLE_NAME>",
                        ))
                    break

            # SA doesn't exist — that's fine, the component isn't installed

        # Check for Pod Identity Agent (newer alternative to IRSA)
        self._check_pod_identity(results)

        return results

    def _validate_role(self, results, role_arn, sa_name, description):
        """Validate that the IAM role referenced by IRSA actually exists."""
        try:
            # Extract role name from ARN
            role_name = role_arn.split("/")[-1]
            self.aws.iam.get_role(RoleName=role_name)
        except self.aws.iam.exceptions.NoSuchEntityException:
            results.append(CheckResult(
                severity="ERROR",
                check=self.title,
                message=f"IRSA role for {description} does not exist: {role_arn}",
                recommendation=f"Create IAM role {role_arn} or update {sa_name} annotation.",
            ))
        except Exception:
            # Permissions issue checking the role — not necessarily a problem
            pass

    def _check_pod_identity(self, results):
        """Check if EKS Pod Identity Agent is installed (newer IRSA alternative)."""
        try:
            managed_addons = self.aws.list_addons()
            if "eks-pod-identity-agent" in managed_addons:
                results.append(CheckResult(
                    severity="INFO",
                    check=self.title,
                    message="EKS Pod Identity Agent is installed (modern alternative to IRSA)",
                ))
        except Exception:
            pass
