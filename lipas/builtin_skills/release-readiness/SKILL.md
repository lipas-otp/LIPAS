---
name: release-readiness
description: Assess a release candidate and produce an evidence-based go, conditional-go, or no-go recommendation.
category: engineering
authority: instructions-only
---
# Release readiness

Treat release as a decision supported by evidence, not as a command to publish.

1. Identify the exact target version, baseline, changed components, supported platforms, and rollback expectations.
2. Review user-visible changes, migrations, compatibility, configuration, dependency, packaging, security, and operational impact.
3. Run only approved checks and record their exact result. Distinguish skipped, unavailable, flaky, and failed verification.
4. Confirm version sources, release notes, documentation, artifacts, and upgrade instructions agree.
5. Surface blockers, accepted risks, required follow-ups, and rollback signals. Give a go, conditional-go, or no-go recommendation with reasons.
6. Do not tag, upload, deploy, push, publish, or announce a release without separate Tools and explicit approval.

This Skill grants no repository or release-system authority.
