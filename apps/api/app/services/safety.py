"""Protection Engine 2.0. Sourced decisions, durable review holds, no unsafe auto-resume."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta

from app.core.config import settings
from app.core.db import aware, utcnow
from app.core.security import audit
from app.models.models import (
    AccountRisk,
    Command,
    Control,
    IntegrityCheck,
    Job,
    Node,
    NodeSafety,
    ProtectionEngineState,
    SafetyDecision,
    SafetyPolicy,
    Session,
    SteamAccount,
)
from app.schemas.safety import IntegrityEvidence, LocalSafetyEvidence
from app.services.compliance import ACTIVE, ProtectionDenied
from app.services.protection import REMEDIES as INCIDENT_REMEDIES
from app.services.protection import enforce, incident, resolve_condition, safe_message
from fastapi import HTTPException
from sqlalchemy import func, select, text, update

REMEDIES = {
    "OVERHEATING": "Stop managed work, inspect cooling and sensor readings. Release only after a fresh safe reading and administrator review.",
    "LOW_MEMORY": "Close unnecessary applications or add RAM. Review and release the affected node after resources recover.",
    "LOW_DISK": "Free disk space, check logs and backups, then explicitly review the node.",
    "SUSTAINED_CPU": "Inspect CPU load and reduce resource limits. No automatic release after a critical hold.",
    "COMPONENT_INTEGRITY_FAILED": "Repair or reinstall the official Farm Manager/Agent package; inspect hash/signature evidence.",
    "GAME_FILES_CHANGED": "Use the official Steam Verify integrity workflow. Recapture an operator baseline only after verifying the installation.",
    "STOP_RETRY_EXHAUSTED": "Inspect the agent and owned processes. An administrator must review the node; automatic stop retries are exhausted.",
    "LOCAL_SAFETY_HOLD": "Review the local guard findings, run an integrity check and explicitly release the node.",
    "AGENT_VERSION_UNSUPPORTED": "Update the agent using the graphical installer, verify its version and explicitly review the node.",
    "AGENT_UPDATE_FAILED": "Inspect the local update failure and rollback diagnostics; repair using the graphical installer.",
    "CONFIGURATION_CORRUPT": "Repair the configuration through the application wizard; do not replace secrets or blindly retry.",
    "REPEATED_CLIENT_FAILURE": "Inspect client errors and installation. Confirm manual authorization again before retrying.",
}

INCIDENT_REMEDIES.update(REMEDIES)


def get_policy(db):
    row = db.get(SafetyPolicy, 1)
    if row is None:
        row = SafetyPolicy(id=1)
        db.add(row)
        db.flush()
    return row


def state(db, node_id):
    row = db.get(NodeSafety, node_id)
    if row is None:
        row = NodeSafety(node_id=node_id)
        db.add(row)
        db.flush()
    return row


def record_decision(
    db,
    action,
    reason,
    *,
    node_id=None,
    account_id=None,
    job_id=None,
    event=None,
    source="safety_engine",
    evidence=None,
    actor="safety-engine",
    key=None,
):
    identity = (
        key
        or f"{event.id if event else 'manual'}:{action}:{node_id}:{account_id}:{job_id}"
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    if db.scalar(
        select(SafetyDecision.id).where(SafetyDecision.deduplication_key == digest)
    ):
        return
    row = SafetyDecision(
        action=action,
        reason=reason,
        node_id=node_id,
        account_id=account_id,
        job_id=job_id,
        incident_id=event.id if event else None,
        source=source,
        actor=actor,
        evidence=evidence or {},
        deduplication_key=digest,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        actor,
        "safety.decision",
        row.id,
        {"action": action, "reason": reason, "node_id": node_id, "job_id": job_id},
    )
    return row


def resource_problems(metrics, rules):
    """Only present, finite observed values; unknown temperature is never guessed."""

    def number(key):
        value = metrics.get(key)
        return (
            float(value)
            if type(value) in (int, float) and math.isfinite(value)
            else None
        )

    reasons = []
    cpu_temp, gpu_temp = number("cpu_temperature"), number("gpu_temperature")
    if (cpu_temp is not None and cpu_temp >= rules.cpu_temperature_limit) or (
        gpu_temp is not None and gpu_temp >= rules.gpu_temperature_limit
    ):
        reasons.append("OVERHEATING")
    ram, available = number("ram_percent"), number("ram_available_gb")
    if (ram is not None and ram >= rules.ram_percent_limit) or (
        available is not None and available < rules.min_ram_available_gb
    ):
        reasons.append("LOW_MEMORY")
    disk = number("disk_free_gb")
    if disk is not None and disk < rules.min_disk_free_gb:
        reasons.append("LOW_DISK")
    return reasons


def fresh(node):
    return bool(
        node.last_heartbeat
        and aware(node.last_heartbeat)
        > utcnow() - timedelta(seconds=settings().node_offline_seconds)
        and not node.revoked
    )


def ingest_integrity(db, node, raw):
    evidence = IntegrityEvidence.model_validate(raw)
    if (
        evidence.source != "agent_self_check"
        or evidence.checked_at.tzinfo is None
        or not -60 <= (utcnow() - evidence.checked_at).total_seconds() <= 86400
    ):
        raise ValueError(
            "Integrity evidence must have a recent timezone-aware agent observation"
        )
    payload = evidence.model_dump(mode="json")
    for finding in payload["findings"]:
        finding["explanation"] = safe_message(finding["explanation"])[:300]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    safety = state(db, node.id)
    if safety.last_integrity_id:
        previous = db.get(IntegrityCheck, safety.last_integrity_id)
        if previous and previous.fingerprint == fingerprint:
            return previous
        if previous and aware(previous.checked_at) > evidence.checked_at:
            return previous
    row = IntegrityCheck(
        node_id=node.id,
        status=evidence.status,
        source=evidence.source,
        checked_at=evidence.checked_at,
        report=payload,
        fingerprint=fingerprint,
    )
    db.add(row)
    db.flush()
    safety.last_integrity_id = row.id
    return row


def ingest_local_safety(raw):
    evidence = LocalSafetyEvidence.model_validate(raw)
    allowed = set(REMEDIES) | {"AGENT_CRASH", "LOCAL_MONITOR_FAILED"}
    if (
        any(r not in allowed for r in evidence.reasons)
        or evidence.observed_at.tzinfo is None
        or not -60 <= (utcnow() - evidence.observed_at).total_seconds() <= 86400
        or evidence.held != bool(evidence.reasons)
    ):
        raise ValueError("Invalid local safety evidence")
    return evidence.model_dump(mode="json")


def quarantine(db, node, safety, reasons, source="telemetry", actor="safety-engine"):
    from app.services.scheduler import cancel_job

    now = utcnow()
    was_held = safety.state in ("REVIEW_REQUIRED", "RELEASING")
    new_reasons = set(reasons) - set(safety.reasons or [])
    safety.state = "REVIEW_REQUIRED"
    safety.release_command_id = None
    safety.reasons = sorted(set(safety.reasons or []) | set(reasons))
    if not was_held:
        safety.triggered_at = now
    if not was_held or new_reasons:
        command(db, node, "safety_hold", {"reasons": sorted(set(reasons))})
    for code in reasons:
        event = incident(
            db,
            code,
            "CRITICAL",
            node_id=node.id,
            source=source,
            evidence={
                "observed_at": aware(node.last_heartbeat).isoformat()
                if node.last_heartbeat
                else None,
                "metrics": {
                    k: node.metrics.get(k)
                    for k in (
                        "cpu_temperature",
                        "gpu_temperature",
                        "ram_percent",
                        "ram_available_gb",
                        "disk_free_gb",
                        "cpu_percent",
                    )
                },
                "integrity_check_id": safety.last_integrity_id,
                "remedy": REMEDIES.get(code, "Review Protection"),
            },
        )
        record_decision(
            db,
            "QUARANTINE_NODE",
            code,
            node_id=node.id,
            event=event,
            source=source,
            actor=actor,
        )
        # Pending assigned work is cancelled; unassigned work can still use healthy nodes.
        active_jobs = {
            s.job_id
            for s in db.scalars(
                select(Session).where(
                    Session.node_id == node.id, Session.status.in_(ACTIVE)
                )
            )
        }
        jobs = list(
            db.scalars(
                select(Job).where(
                    Job.status.in_(("QUEUED", "STARTING", "RUNNING", "UNKNOWN")),
                    (Job.node_id == node.id) | (Job.id.in_(active_jobs)),
                )
            )
        )
        for job in jobs:
            cancel_job(db, job, "safety-engine")
            job.error = "Safety: " + code
            record_decision(
                db,
                "STOP_AFFECTED_JOB",
                code,
                node_id=node.id,
                account_id=job.account_id,
                job_id=job.id,
                event=event,
                source=source,
                actor=actor,
            )
    db.execute(
        update(AccountRisk)
        .where(
            AccountRisk.auth_node_id == node.id, AccountRisk.auth_status == "AUTHORIZED"
        )
        .values(
            auth_status="UNKNOWN",
            auth_expires_at=None,
            auth_source="safety_engine",
            auth_checked_at=now,
        )
    )


def assess_node(db, node, rules):
    safety = state(db, node.id)
    safety.evaluated_at = utcnow()
    if not fresh(node):
        safety.cpu_high_since = None
        if safety.state not in ("REVIEW_REQUIRED", "RELEASING"):
            safety.state = "UNKNOWN"
        return safety
    reasons = resource_problems(node.metrics, rules)
    cpu = node.metrics.get("cpu_percent")
    if type(cpu) in (int, float) and cpu >= rules.cpu_percent_limit:
        if not safety.cpu_high_since:
            safety.cpu_high_since = utcnow()
        if (
            utcnow() - aware(safety.cpu_high_since)
        ).total_seconds() >= rules.sustained_cpu_seconds:
            reasons.append("SUSTAINED_CPU")
    else:
        safety.cpu_high_since = None
    integrity = (
        db.get(IntegrityCheck, safety.last_integrity_id)
        if safety.last_integrity_id
        else None
    )
    if integrity and integrity.status == "FAILED":
        codes = {f["code"] for f in integrity.report.get("findings", [])}
        if "AGENT_UPDATE_FAILED" in codes:
            reasons.append("AGENT_UPDATE_FAILED")
        if integrity.report.get("game_files_status") == "CHANGED":
            reasons.append("GAME_FILES_CHANGED")
        elif "AGENT_UPDATE_FAILED" not in codes or any(
            f["status"] == "FAILED" and f["code"] != "AGENT_UPDATE_FAILED"
            for f in integrity.report.get("findings", [])
        ):
            reasons.append("COMPONENT_INTEGRITY_FAILED")
    local = node.metrics.get("local_safety") or {}
    if local.get("held") and safety.state != "RELEASING":
        reasons.extend(local.get("reasons") or ["LOCAL_SAFETY_HOLD"])
    if rules.minimum_agent_version:
        try:
            if tuple(map(int, node.agent_version.split("."))) < tuple(
                map(int, rules.minimum_agent_version.split("."))
            ):
                reasons.append("AGENT_VERSION_UNSUPPORTED")
        except ValueError:
            reasons.append("AGENT_VERSION_UNSUPPORTED")
    if reasons:
        quarantine(db, node, safety, sorted(set(reasons)))
    elif safety.state not in ("REVIEW_REQUIRED", "RELEASING"):
        verified = bool(
            integrity
            and integrity.status == "PASS"
            and aware(integrity.checked_at)
            > utcnow() - timedelta(seconds=rules.max_integrity_age_seconds)
        )
        safety.state = "READY" if verified else "WARNING"
        safety.reasons = []
    return safety


def assert_node_allowed(db, node_id, kind=None):
    if not node_id:
        return
    safety = db.get(NodeSafety, node_id)
    if safety and safety.state in ("REVIEW_REQUIRED", "RELEASING"):
        raise ProtectionDenied("NODE_REVIEW_REQUIRED", node_id=node_id)
    if kind in ("steam", "cs2") and get_policy(db).require_integrity_for_games:
        integrity = (
            db.get(IntegrityCheck, safety.last_integrity_id)
            if safety and safety.last_integrity_id
            else None
        )
        if (
            not integrity
            or integrity.report.get("files_checked", 0) < 1
            or integrity.status not in ("PASS", "WARNING")
            or aware(integrity.checked_at)
            < utcnow() - timedelta(seconds=get_policy(db).max_integrity_age_seconds)
        ):
            raise ProtectionDenied("FRESH_INTEGRITY_REQUIRED", node_id=node_id)


def command(db, node, operation, payload=None):
    existing = db.scalar(
        select(Command).where(
            Command.node_id == node.id,
            Command.operation == operation,
            Command.ack_status.is_(None),
            Command.deadline > utcnow(),
        )
    )
    if existing:
        return existing
    row = Command(
        node_id=node.id,
        operation=operation,
        payload={"payload": payload or {}},
        deadline=utcnow() + timedelta(minutes=10),
    )
    db.add(row)
    db.flush()
    return row


def request_release(db, node, actor, note):
    safety = state(db, node.id)
    if not fresh(node):
        raise HTTPException(409, "A fresh node connection is required before release")
    rules = get_policy(db)
    if db.scalar(
        select(Command.id).where(
            Command.node_id == node.id,
            Command.operation == "safety_hold",
            Command.ack_status.is_(None),
            Command.deadline > utcnow(),
        )
    ):
        raise HTTPException(
            409, "Wait for the local guard to acknowledge the hold before release"
        )
    if (
        resource_problems(node.metrics, rules)
        or node.metrics.get("cpu_percent", 0) >= rules.cpu_percent_limit
    ):
        raise HTTPException(409, "Unsafe resource readings still present")
    integrity = (
        db.get(IntegrityCheck, safety.last_integrity_id)
        if safety.last_integrity_id
        else None
    )
    if (
        not integrity
        or integrity.status == "FAILED"
        or aware(integrity.checked_at)
        < utcnow() - timedelta(seconds=rules.max_integrity_age_seconds)
    ):
        raise HTTPException(
            409,
            "Run a fresh integrity check and repair reported failures before release",
        )
    if safety.state == "RELEASING":
        return safety
    if safety.state != "REVIEW_REQUIRED":
        raise HTTPException(409, "Node does not require release")
    row = command(db, node, "safety_release")
    safety.state = "RELEASING"
    safety.release_command_id = row.id
    safety.reviewed_by = actor
    safety.reviewed_at = utcnow()
    record_decision(
        db,
        "REQUEST_NODE_RELEASE",
        "ADMIN_REVIEW",
        node_id=node.id,
        source="operator",
        actor=actor,
        evidence={"note": safe_message(note)},
        key="release:" + row.id,
    )
    return safety


def handle_control_ack(db, node, command_row, data):
    if (
        command_row.operation
        in ("integrity_check", "game_baseline_capture", "repair_acknowledge")
        and data.get("status") == "STOPPED"
    ):
        report = data.get("resource_usage", {}).get("integrity")
        if report:
            ingest_integrity(db, node, report)
    if command_row.operation != "safety_release":
        return
    safety = state(db, node.id)
    if safety.release_command_id != command_row.id:
        return
    if data.get("status") != "STOPPED":
        safety.state = "REVIEW_REQUIRED"
        safety.release_command_id = None
        record_decision(
            db,
            "RELEASE_REJECTED",
            "LOCAL_GUARD_REJECTED",
            node_id=node.id,
            key="rejected:" + command_row.id,
        )
        return
    report = data.get("resource_usage", {}).get("integrity")
    local = data.get("resource_usage", {}).get("safety")
    if not report or not local or ingest_local_safety(local)["held"]:
        safety.state = "REVIEW_REQUIRED"
        return
    checked = ingest_integrity(db, node, report)
    if checked.status == "FAILED" or resource_problems(node.metrics, get_policy(db)):
        safety.state = "REVIEW_REQUIRED"
        return
    node.metrics = {**node.metrics, "local_safety": ingest_local_safety(local)}
    previous_reasons = list(safety.reasons)
    safety.cpu_high_since = None
    safety.state = "READY" if checked.status == "PASS" else "WARNING"
    safety.reasons = []
    safety.release_command_id = None
    for code in previous_reasons:
        resolve_condition(db, code, node_id=node.id)
    record_decision(
        db,
        "RELEASE_NODE",
        "ADMIN_AND_LOCAL_CHECKS_PASSED",
        node_id=node.id,
        source="agent_and_operator",
        actor=safety.reviewed_by or "admin",
        key="released:" + command_row.id,
    )
    # No cancelled job is requeued and no Steam login is re-attested by release.


def cycle(db):
    if db.bind.dialect.name == "postgresql" and not db.scalar(
        text("SELECT pg_try_advisory_xact_lock(73294021)")
    ):
        return {"status": "BUSY"}
    rules = get_policy(db)
    nodes = list(db.scalars(select(Node).where(Node.revoked == False)))
    loaded_states = list(db.scalars(select(NodeSafety)))
    _loaded_checks = list(
        db.scalars(
            select(IntegrityCheck).where(
                IntegrityCheck.id.in_(
                    [s.last_integrity_id for s in loaded_states if s.last_integrity_id]
                )
            )
        )
    )
    for node in nodes:
        assess_node(db, node, rules)
    # Repeated managed client failures require review, not an unlimited relaunch loop.
    since = utcnow() - timedelta(minutes=15)
    failed = db.execute(
        select(Session.account_id, Session.node_id, func.count(Session.id))
        .join(Job, Job.id == Session.job_id)
        .where(
            Session.status == "FAILED",
            Session.ended_at >= since,
            Job.kind.in_(("steam", "cs2")),
            Session.account_id.is_not(None),
        )
        .group_by(Session.account_id, Session.node_id)
        .having(func.count(Session.id) >= 3)
    ).all()
    for account_id, node_id, count in failed:
        risk = db.get(AccountRisk, account_id)
        if risk and not risk.manual_hold:
            risk.manual_hold = True
            event = incident(
                db,
                "REPEATED_CLIENT_FAILURE",
                "HIGH",
                account_id=account_id,
                node_id=node_id,
                source="managed_process_results",
                evidence={"failures_15m": count},
            )
            record_decision(
                db,
                "HOLD_ACCOUNT",
                "REPEATED_CLIENT_FAILURE",
                account_id=account_id,
                node_id=node_id,
                event=event,
            )
    enforce(db)
    inspect_manager(db)
    engine = db.get(ProtectionEngineState, 1)
    if not engine:
        engine = ProtectionEngineState(id=1)
        db.add(engine)
    engine.last_cycle_at = utcnow()
    engine.last_error = None
    engine.nodes_evaluated = len(nodes)
    engine.accounts_evaluated = db.scalar(
        select(func.count(SteamAccount.id)).where(SteamAccount.archived == False)
    )
    return {
        "status": "SUCCESS",
        "nodes": len(nodes),
        "accounts": engine.accounts_evaluated,
    }


def validate_metrics(raw):
    limits = {
        "cpu_percent": (0, 100),
        "gpu_percent": (0, 100),
        "ram_percent": (0, 100),
        "disk_percent": (0, 100),
        "cpu_temperature": (-20, 150),
        "gpu_temperature": (-20, 150),
        "ram_available_gb": (0, 1000000),
        "disk_free_gb": (0, 10000000),
    }
    if not isinstance(raw, dict):
        raise TypeError("Metrics must be an object")
    result = {}
    for key, value in raw.items():
        if key in limits:
            low, high = limits[key]
            if value is not None and (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise ValueError("Invalid finite telemetry metric: " + key)
            result[key] = value
        elif key in ("gpu_name", "cpu_temperature_source", "gpu_temperature_source"):
            if value is not None and (not isinstance(value, str) or len(value) > 255):
                raise ValueError("Invalid telemetry source")
            result[key] = value
    return result


def _inspect_manager(db, force=False):
    from pathlib import Path

    previous = db.scalar(
        select(IntegrityCheck)
        .where(IntegrityCheck.node_id.is_(None))
        .order_by(IntegrityCheck.checked_at.desc())
        .limit(1)
    )
    if (
        not force
        and previous
        and aware(previous.checked_at) > utcnow() - timedelta(seconds=300)
    ):
        return previous
    import sys

    root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2]
    )
    manifest = root / "component-integrity.json"
    findings = []
    count = 0
    trust = "not_available"
    if manifest.exists():
        try:
            value = json.loads(manifest.read_text())
            files = value["files"]
            if (
                value.get("schema") != 1
                or not isinstance(files, dict)
                or not 1 <= len(files) <= 20000
            ):
                raise ValueError("Invalid component manifest")
            for name, expected in files.items():
                target = (root / name).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("Unsafe baseline path")
                count += 1
                if not target.is_file():
                    raise ValueError("Missing component")
                with target.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != expected:
                    raise ValueError("Changed component")
            for name, expected in value.get("desktop_files", {}).items():
                resources = root.parent.parent
                target = (resources / name).resolve()
                if not target.is_relative_to(resources) or not target.is_file():
                    raise ValueError("Missing desktop component")
                with target.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != expected:
                    raise ValueError("Changed desktop component")
                count += 1
            trust = "unsigned_local_manifest"
            findings = [
                {
                    "code": "COMPONENT_HASHES_MATCH",
                    "status": "PASS",
                    "component": "manager",
                    "explanation": f"{count} packaged files match the local baseline",
                },
                {
                    "code": "BASELINE_NOT_AUTHENTICATED",
                    "status": "WARNING",
                    "component": "manager",
                    "explanation": "Local hash baseline; publisher authentication depends on the installer/image signature",
                },
            ]
            result = "WARNING"
        except (ValueError, OSError, KeyError, TypeError):
            result = "FAILED"
            findings = [
                {
                    "code": "COMPONENT_HASH_MISMATCH",
                    "status": "FAILED",
                    "component": "manager",
                    "explanation": "A packaged component or its local baseline is missing, changed or invalid",
                }
            ]
    else:
        result = "UNKNOWN"
        findings = [
            {
                "code": "BASELINE_UNAVAILABLE",
                "status": "UNKNOWN",
                "component": "manager",
                "explanation": "No packaged integrity baseline in this source/development deployment",
            }
        ]
    report = {
        "status": result,
        "source": "manager_self_check",
        "checked_at": utcnow().isoformat(),
        "baseline_trust": trust,
        "files_checked": count,
        "findings": findings,
        "game_files_status": "UNKNOWN",
    }
    row = IntegrityCheck(
        node_id=None,
        status=result,
        source="manager_self_check",
        checked_at=utcnow(),
        report=report,
        fingerprint=hashlib.sha256(
            json.dumps(report, sort_keys=True).encode()
        ).hexdigest(),
    )
    db.add(row)
    db.flush()
    return row


def inspect_manager(db, force=False):
    row = _inspect_manager(db, force=force)
    if row.status == "FAILED":
        from app.services.scheduler import cancel_job

        control = db.get(Control, 1)
        if control:
            control.paused = True
        event = incident(
            db,
            "MANAGER_COMPONENT_INTEGRITY_FAILED",
            "CRITICAL",
            source="manager_self_check",
            evidence={
                "check_id": row.id,
                "remedy": "Repair using the genuine installer. The queue will not resume automatically.",
            },
        )
        record_decision(
            db,
            "PAUSE_QUEUE",
            "MANAGER_COMPONENT_INTEGRITY_FAILED",
            event=event,
            source="manager_self_check",
        )
        for job in db.scalars(
            select(Job).where(
                Job.status.in_(("QUEUED", "STARTING", "RUNNING", "UNKNOWN"))
            )
        ):
            cancel_job(db, job, "safety-engine")
            record_decision(
                db,
                "STOP_AFFECTED_JOB",
                "MANAGER_COMPONENT_INTEGRITY_FAILED",
                job_id=job.id,
                event=event,
                source="manager_self_check",
            )
    return row


def assert_manager_allowed(db):
    latest = db.scalar(
        select(IntegrityCheck)
        .where(IntegrityCheck.node_id.is_(None))
        .order_by(IntegrityCheck.checked_at.desc())
        .limit(1)
    )
    if latest and latest.status == "FAILED":
        raise ProtectionDenied("MANAGER_COMPONENT_INTEGRITY_FAILED")
