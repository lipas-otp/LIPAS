---
name: calendar-planning
description: Draft schedules and calendar changes while preserving constraints, timezones, scope, and attendee choice.
category: office
authority: draft-only
---
# Calendar planning

Separate schedule reasoning from provider updates.

1. Establish purpose, participants, duration, timezone, date range, recurrence, location, preparation, and hard versus soft constraints.
2. Make timezone and daylight-saving assumptions explicit. Do not treat missing availability as free time.
3. Detect overlaps, travel or transition time, focus-time impact, working-hour boundaries, and recurrence edge cases.
4. Return a proposed schedule or event preview with unresolved conflicts and alternatives.
5. Do not invite attendees, change events, reserve rooms, or imply acceptance without a calendar Tool and approval.
6. For real updates, preserve a stable event identity and reconcile an uncertain provider result before retrying.

This Skill supplies planning method only and grants no calendar access.
