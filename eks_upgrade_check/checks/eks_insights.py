"""Pulls upgrade readiness insights from the EKS API."""

from eks_upgrade_check.checks import BaseCheck
from eks_upgrade_check.reporter import CheckResult


# maps AWS insight statuses to our severity levels
INSIGHT_STATUS_MAP = {
    "PASSING": "PASS",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "UNKNOWN": "INFO",
}


class EKSInsightsCheck(BaseCheck):
    """Fetches EKS upgrade insights — deprecated API usage from audit logs,
    addon compatibility, kube-proxy skew, etc. Re-evaluates severity
    based on our target version since AWS rates relative to current."""

    title = "EKS Upgrade Insights (AWS)"

    def run(self, context: dict) -> list[CheckResult]:
        results = []
        target = context["target_version"]
        current = context["current_version"]
        target_minor = int(target.split(".")[1])

        # Fetch insights from the EKS API
        try:
            insights = self.aws.list_insights()
        except Exception as e:
            results.append(CheckResult(
                severity="WARNING",
                check=self.title,
                message=f"Could not retrieve EKS insights: {e}",
                detail="Ensure your IAM permissions include eks:ListInsights and eks:DescribeInsight.",
                recommendation="Grant eks:ListInsights and eks:DescribeInsight permissions.",
            ))
            return results

        if not insights:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message="No EKS insights returned for this cluster",
                detail="Insights may not yet be available. EKS scans clusters once every 24 hours.",
            ))
            return results

        # Filter to UPGRADE_READINESS insights
        upgrade_insights = [
            i for i in insights
            if i.get("category") == "UPGRADE_READINESS"
        ]
        config_insights = [
            i for i in insights
            if i.get("category") != "UPGRADE_READINESS"
        ]

        if not upgrade_insights:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message="No upgrade readiness insights found",
            ))
        else:
            results.append(CheckResult(
                severity="INFO",
                check=self.title,
                message=f"Found {len(upgrade_insights)} upgrade readiness insight(s) from EKS",
            ))

        # Process each upgrade insight
        for insight in upgrade_insights:
            insight_id = insight.get("id", "unknown")
            name = insight.get("name", "Unknown insight")
            status_obj = insight.get("insightStatus", {})
            status = status_obj.get("status", "UNKNOWN")
            reason = status_obj.get("reason", "")
            k8s_version = insight.get("kubernetesVersion", "")
            description = insight.get("description", "")

            severity = INSIGHT_STATUS_MAP.get(status, "INFO")

            # Re-evaluate severity based on the user's target version.
            # AWS rates severity relative to the current cluster version,
            # but we rate it relative to the target version.
            if k8s_version and severity in ("WARNING", "ERROR"):
                try:
                    insight_minor = int(k8s_version.split(".")[1])
                    if insight_minor > target_minor + 1:
                        # This insight is about a version well beyond our target
                        severity = "INFO"
                    elif insight_minor <= target_minor and severity == "WARNING":
                        # AWS says WARNING (because it's N+2 from current), but
                        # the removal version is within our target — escalate to ERROR
                        severity = "ERROR"
                except (ValueError, IndexError):
                    pass

            # Try to get detailed insight info (affected resources)
            detail_lines = []
            recommendation = ""

            try:
                detail = self.aws.describe_insight(insight_id)
                insight_detail = detail.get("insight", {})

                # Get remediation recommendation
                recommendation = insight_detail.get("recommendation", "")

                # Get affected resources
                resources = insight_detail.get("resources", [])
                if resources:
                    detail_lines.append(f"Affected resources ({len(resources)}):")
                    for res in resources[:10]:
                        res_status = res.get("insightStatus", {}).get("status", "")
                        res_arn = res.get("kubernetesResourceUri", res.get("arn", "unknown"))
                        detail_lines.append(f"  [{res_status}] {res_arn}")
                    if len(resources) > 10:
                        detail_lines.append(f"  ... and {len(resources) - 10} more")

                # Get additional context from summary
                summary = insight_detail.get("categorySpecificSummary", {})
                deprecation_details = summary.get("deprecationDetails", [])
                if deprecation_details:
                    detail_lines.append("")
                    detail_lines.append("Deprecation details:")
                    for dep in deprecation_details[:5]:
                        usage = dep.get("usage", "")
                        client_stats = dep.get("clientStats", [])
                        replaced_with = dep.get("replacedWith", "")
                        start_serving = dep.get("startServingReplacementVersion", "")
                        stop_serving = dep.get("stopServingVersion", "")

                        line = f"  {usage}"
                        if replaced_with:
                            line += f" → {replaced_with}"
                        if stop_serving:
                            line += f" (removed in {stop_serving})"
                        detail_lines.append(line)

                        # Show which user agents are making deprecated calls
                        if client_stats:
                            for cs in client_stats[:3]:
                                user_agent = cs.get("userAgent", "unknown")
                                last_time = cs.get("lastRequestTime", "")
                                detail_lines.append(
                                    f"    Last called by: {user_agent}"
                                )

            except Exception:
                # Couldn't get detailed insight — still report the summary
                if reason:
                    detail_lines.append(reason)

            # Build the result
            message = f"{name}"
            if k8s_version:
                message += f" (K8s {k8s_version})"

            results.append(CheckResult(
                severity=severity,
                check=self.title,
                message=message,
                detail="\n".join(detail_lines) if detail_lines else reason,
                recommendation=recommendation,
                resource=insight_id,
            ))

        # Also surface configuration insights if any have issues
        for insight in config_insights:
            status_obj = insight.get("insightStatus", {})
            status = status_obj.get("status", "UNKNOWN")
            if status in ("ERROR", "WARNING"):
                name = insight.get("name", "Unknown")
                reason = status_obj.get("reason", "")
                severity = INSIGHT_STATUS_MAP.get(status, "INFO")

                results.append(CheckResult(
                    severity=severity,
                    check=self.title,
                    message=f"Configuration insight: {name}",
                    detail=reason,
                ))

        # Report last refresh time
        if upgrade_insights:
            latest_refresh = max(
                (i.get("lastRefreshTime", 0) for i in upgrade_insights),
                default=0,
            )
            if latest_refresh:
                from datetime import datetime
                try:
                    if isinstance(latest_refresh, (int, float)):
                        refresh_dt = datetime.utcfromtimestamp(latest_refresh)
                    else:
                        refresh_dt = latest_refresh
                    results.append(CheckResult(
                        severity="INFO",
                        check=self.title,
                        message=f"Insights last refreshed: {refresh_dt.strftime('%Y-%m-%d %H:%M UTC')}",
                        detail="EKS refreshes insights automatically every 24 hours.",
                    ))
                except Exception:
                    pass

        return results
