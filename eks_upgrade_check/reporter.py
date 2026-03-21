"""Handles output formatting — terminal colours, JSON, and Markdown reports."""

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from datetime import datetime


class ReportFormat(Enum):
    TERMINAL = "terminal"
    JSON = "json"
    MARKDOWN = "markdown"


@dataclass
class CheckResult:
    severity: str          # PASS, WARNING, ERROR, INFO
    check: str             # which check produced this
    message: str           # what we found
    detail: str = ""       # extra context
    recommendation: str = ""  # what to do about it
    resource: str = ""     # affected resource
    fix: str = ""          # copy-paste fix command


class Colours:
    """ANSI codes."""
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


SEVERITY_COLOURS = {
    "PASS": Colours.GREEN,
    "WARNING": Colours.YELLOW,
    "ERROR": Colours.RED,
    "INFO": Colours.CYAN,
}

SEVERITY_ICONS = {
    "PASS": "✓",
    "WARNING": "⚠",
    "ERROR": "✗",
    "INFO": "ℹ",
}


class Reporter:
    """Formats and prints check results to terminal, JSON, or Markdown."""

    def __init__(self, fmt: ReportFormat = ReportFormat.TERMINAL):
        self.fmt = fmt

    def print_banner(self, cluster: str, target: str):
        """Print the tool banner."""
        if self.fmt != ReportFormat.TERMINAL:
            return
        print()
        print(f"{Colours.BOLD}╔══════════════════════════════════════════════════╗{Colours.RESET}")
        print(f"{Colours.BOLD}║       EKS Upgrade Readiness Checker v0.1.0       ║{Colours.RESET}")
        print(f"{Colours.BOLD}╚══════════════════════════════════════════════════╝{Colours.RESET}")
        print()

    def print_info(self, message: str):
        """Print an info line."""
        if self.fmt != ReportFormat.TERMINAL:
            return
        print(f"  {Colours.GREY}{message}{Colours.RESET}")

    def print_separator(self):
        """Print a section separator."""
        if self.fmt != ReportFormat.TERMINAL:
            return
        print()

    def print_check_header(self, title: str):
        """Print a check section header."""
        if self.fmt != ReportFormat.TERMINAL:
            return
        print(f"  {Colours.BOLD}{title}{Colours.RESET}")
        print(f"  {'─' * 48}")

    def print_result(self, result: CheckResult):
        """Print a single check result."""
        if self.fmt != ReportFormat.TERMINAL:
            return
        colour = SEVERITY_COLOURS.get(result.severity, Colours.RESET)
        icon = SEVERITY_ICONS.get(result.severity, " ")
        print(f"  {colour}[{result.severity:7s}]{Colours.RESET} {icon}  {result.message}")
        if result.detail:
            for line in result.detail.split("\n"):
                print(f"            {Colours.GREY}{line}{Colours.RESET}")

    def print_error(self, message: str):
        """Print an error message."""
        if self.fmt != ReportFormat.TERMINAL:
            return
        print(f"  {Colours.RED}[ERROR]  {message}{Colours.RESET}")

    def print_fatal(self, message: str):
        """Print a fatal error."""
        print(f"\n  {Colours.RED}{Colours.BOLD}FATAL: {message}{Colours.RESET}\n")

    def print_summary(self, results: list):
        """Print a summary of all results."""
        if self.fmt != ReportFormat.TERMINAL:
            return

        passes = sum(1 for r in results if r.severity == "PASS")
        warnings = sum(1 for r in results if r.severity == "WARNING")
        errors = sum(1 for r in results if r.severity == "ERROR")
        infos = sum(1 for r in results if r.severity == "INFO")

        print(f"  {Colours.BOLD}Summary{Colours.RESET}")
        print(f"  {'─' * 48}")
        print(f"  {Colours.GREEN}[PASS]    {passes}{Colours.RESET}")
        print(f"  {Colours.YELLOW}[WARNING] {warnings}{Colours.RESET}")
        print(f"  {Colours.RED}[ERROR]   {errors}{Colours.RESET}")
        print(f"  {Colours.CYAN}[INFO]    {infos}{Colours.RESET}")
        print()

        if errors > 0:
            print(f"  {Colours.RED}{Colours.BOLD}✗ Cluster is NOT ready for upgrade.{Colours.RESET}")
        elif warnings > 0:
            print(f"  {Colours.YELLOW}{Colours.BOLD}⚠ Cluster can be upgraded, but review warnings first.{Colours.RESET}")
        else:
            print(f"  {Colours.GREEN}{Colours.BOLD}✓ Cluster appears ready for upgrade.{Colours.RESET}")

        # Estimated risk
        drain_blockers = sum(
            1 for r in results
            if r.severity == "ERROR" and r.check == "Drain Impact Analysis" and "BLOCKED" in r.message
        )
        if errors > 3 or drain_blockers > 0:
            risk = f"{Colours.RED}HIGH"
            risk_detail = []
            if drain_blockers:
                risk_detail.append(f"{drain_blockers} drain blocker(s)")
            if errors > drain_blockers:
                risk_detail.append(f"{errors - drain_blockers} other error(s)")
            risk += f" ({', '.join(risk_detail)}){Colours.RESET}" if risk_detail else Colours.RESET
        elif errors > 0 or warnings > 5:
            risk = f"{Colours.YELLOW}MEDIUM ({errors} error(s), {warnings} warning(s)){Colours.RESET}"
        else:
            risk = f"{Colours.GREEN}LOW{Colours.RESET}"

        print(f"  Estimated risk: {risk}")
        print()

    def print_recommendations(self, results: list):
        """Print recommended actions from findings."""
        if self.fmt != ReportFormat.TERMINAL:
            return

        recs = [r for r in results if r.recommendation and r.severity in ("ERROR", "WARNING")]
        if not recs:
            return

        # Sort: ERRORs first, then WARNINGs
        recs.sort(key=lambda r: 0 if r.severity == "ERROR" else 1)

        print(f"  {Colours.BOLD}Recommended actions:{Colours.RESET}")
        print()
        for i, r in enumerate(recs, 1):
            colour = SEVERITY_COLOURS.get(r.severity, Colours.RESET)
            print(f"  {colour}{i}. {r.recommendation}{Colours.RESET}")
            if r.fix:
                for line in r.fix.strip().split("\n"):
                    print(f"     {Colours.GREY}$ {line}{Colours.RESET}")
        print()

    def generate_report(self, results: list, context: dict) -> str:
        """Generate a full report as a string (for file output)."""
        if self.fmt == ReportFormat.JSON:
            return self._generate_json(results, context)
        elif self.fmt == ReportFormat.MARKDOWN:
            return self._generate_markdown(results, context)
        return ""

    def _generate_json(self, results: list, context: dict) -> str:
        """Generate JSON report."""
        report = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "cluster": context["cluster_name"],
            "current_version": context["current_version"],
            "target_version": context["target_version"],
            "region": context["region"],
            "summary": {
                "pass": sum(1 for r in results if r.severity == "PASS"),
                "warning": sum(1 for r in results if r.severity == "WARNING"),
                "error": sum(1 for r in results if r.severity == "ERROR"),
                "info": sum(1 for r in results if r.severity == "INFO"),
                "ready": not any(r.severity == "ERROR" for r in results),
            },
            "findings": [asdict(r) for r in results],
            "recommendations": [
                {"priority": i + 1, "severity": r.severity, "action": r.recommendation}
                for i, r in enumerate(
                    sorted(
                        [r for r in results if r.recommendation and r.severity in ("ERROR", "WARNING")],
                        key=lambda r: 0 if r.severity == "ERROR" else 1,
                    )
                )
            ],
        }
        return json.dumps(report, indent=2)

    def _generate_markdown(self, results: list, context: dict) -> str:
        """Generate Markdown report."""
        lines = [
            f"# EKS Upgrade Readiness Report",
            f"",
            f"**Cluster:** {context['cluster_name']}  ",
            f"**Current Version:** {context['current_version']}  ",
            f"**Target Version:** {context['target_version']}  ",
            f"**Region:** {context['region']}  ",
            f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC  ",
            f"",
            f"## Summary",
            f"",
            f"| Severity | Count |",
            f"|----------|-------|",
            f"| PASS | {sum(1 for r in results if r.severity == 'PASS')} |",
            f"| WARNING | {sum(1 for r in results if r.severity == 'WARNING')} |",
            f"| ERROR | {sum(1 for r in results if r.severity == 'ERROR')} |",
            f"| INFO | {sum(1 for r in results if r.severity == 'INFO')} |",
            f"",
            f"## Findings",
            f"",
        ]

        # Group by check
        checks = {}
        for r in results:
            checks.setdefault(r.check, []).append(r)

        for check_name, findings in checks.items():
            lines.append(f"### {check_name}")
            lines.append("")
            for f in findings:
                icon = {"PASS": "✅", "WARNING": "⚠️", "ERROR": "❌", "INFO": "ℹ️"}.get(f.severity, "")
                lines.append(f"- {icon} **[{f.severity}]** {f.message}")
                if f.detail:
                    lines.append(f"  - {f.detail}")
            lines.append("")

        recs = [r for r in results if r.recommendation and r.severity in ("ERROR", "WARNING")]
        if recs:
            recs.sort(key=lambda r: 0 if r.severity == "ERROR" else 1)
            lines.append("## Recommended Actions")
            lines.append("")
            for i, r in enumerate(recs, 1):
                lines.append(f"{i}. **[{r.severity}]** {r.recommendation}")
                if r.fix:
                    lines.append(f"   ```bash")
                    lines.append(f"   {r.fix.strip()}")
                    lines.append(f"   ```")
            lines.append("")

        return "\n".join(lines)
