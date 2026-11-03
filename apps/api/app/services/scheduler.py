"""PostgreSQL leases + persisted commands. Unknown processes retain node reservations."""

import uuid
from datetime import timedelta
from sqlalchemy import select, func, text
from fastapi import HTTPException
from app.core.db import utcnow, aware
from app.core.config import settings
from app.core.security import audit
from app.models.models import (
    Job,
    Node,
    Session,
    JobAttempt,
    Command,
    Control,
    Schedule,
    SteamAccount,
)
from app.schemas.api import JobCreate

ACTIVE = ("STARTING", "RUNNING", "STOPPING", "UNKNOWN")
TERMINAL = ("STOPPED", "FAILED", "TIMED_OUT")
TRANSITIONS = {
    "QUEUED": {"STARTING", "STOPPED"},
    "STARTING": {"RUNNING", "STOPPING", "FAILED", "TIMED_OUT", "UNKNOWN", "STOPPED"},
    "RUNNING": {"STOPPING", "STOPPED", "FAILED", "TIMED_OUT", "UNKNOWN"},
    "STOPPING": {"STOPPED", "FAILED", "TIMED_OUT", "UNKNOWN"},
    "UNKNOWN": {"RUNNING", "STOPPING", "STOPPED", "FAILED", "TIMED_OUT"},
    "STOPPED": set(),
    "FAILED": set(),
    "TIMED_OUT": set(),
}


def transition(session, status):
    if session.status == status:
        return
    if status not in TRANSITIONS.get(session.status, set()):
        raise ValueError(f"Invalid transition {session.status} -> {status}")
    session.status = status
    if status in TERMINAL:
        session.ended_at = utcnow()


def create_job(db, data: JobCreate, key, actor):
    prior = db.scalar(select(Job).where(Job.idempotency_key == key))
    if prior:
        same = (
            prior.kind == data.kind
            and prior.account_id == data.account_id
            and prior.node_id == data.node_id
            and prior.payload == data.payload.model_dump()
            and prior.priority == data.priority
            and prior.max_attempts == data.max_attempts
            and prior.timeout_seconds == data.timeout_seconds
        )
        if not same:
            raise HTTPException(
                409, "Idempotency key already used with different job parameters"
            )
        return prior
    if data.node_id and not db.get(Node, data.node_id):
        raise HTTPException(404, "Node not found")
    if data.account_id:
        account = db.get(SteamAccount, data.account_id)
        if not account or account.archived:
            raise HTTPException(404, "Active account not found")
    if data.kind != "test" and not data.account_id:
        raise HTTPException(
            422, "Steam/CS2 sessions require an account and manual client authorization"
        )
    job = Job(
        **data.model_dump(exclude={"payload", "scheduled_at"}),
        payload=data.payload.model_dump(),
        scheduled_at=data.scheduled_at or utcnow(),
        idempotency_key=key,
    )
    db.add(job)
    db.flush()
    audit(db, actor, "job.create", job.id, {"kind": job.kind})
    return job


def stop_session(db, session, actor="scheduler"):
    if session.status in TERMINAL:
        return
    if session.status != "STOPPING":
        transition(session, "STOPPING")
    existing = db.scalar(
        select(Command).where(
            Command.session_id == session.id, Command.operation == "stop"
        )
    )
    if not existing:
        db.add(
            Command(
                node_id=session.node_id,
                session_id=session.id,
                operation="stop",
                payload={},
                deadline=utcnow() + timedelta(days=7),
            )
        )
        audit(db, actor, "session.stop", session.id)


def cancel_job(db, job, actor):
    if job.status in ("COMPLETED", "CANCELLED"):
        return
    job.status = "CANCELLED"
    for session in db.scalars(
        select(Session).where(Session.job_id == job.id, Session.status.in_(ACTIVE))
    ):
        stop_session(db, session, actor)
    audit(db, actor, "job.cancel", job.id)


def retry_job(db, job, actor):
    if job.status not in ("FAILED", "TIMED_OUT"):
        raise HTTPException(409, "Only failed or timed-out jobs can be retried")
    if db.scalar(
        select(Session.id).where(Session.job_id == job.id, Session.status.in_(ACTIVE))
    ):
        raise HTTPException(409, "Unresolved session still reserves node")
    job.status = "QUEUED"
    job.attempts = 0
    job.scheduled_at = utcnow()
    job.error = None
    audit(db, actor, "job.retry", job.id)


def finish(db, session, status, error=None, exit_code=None):
    transition(session, status)
    session.error = error
    session.exit_code = exit_code
    job = db.get(Job, session.job_id)
    attempt = db.scalar(select(JobAttempt).where(JobAttempt.session_id == session.id))
    if attempt:
        attempt.ended_at = utcnow()
        attempt.error = error
    job.lease_until = None
    job.lease_owner = None
    if job.status == "CANCELLED":
        return
    if status == "STOPPED" and (exit_code is None or exit_code == 0):
        job.status = "COMPLETED"
        job.error = None
    elif job.attempts < job.max_attempts:
        job.status = "QUEUED"
        job.error = error or status
        job.scheduled_at = utcnow() + timedelta(seconds=min(300, 5 * 2**job.attempts))
    else:
        job.status = "TIMED_OUT" if status == "TIMED_OUT" else "FAILED"
        job.error = error or status
    from app.services.notifications import notify

    notify(
        db,
        "job_completed" if job.status == "COMPLETED" else "session_failed",
        f"Job {job.id}: {job.status}",
        "info" if job.status == "COMPLETED" else "error",
    )
    audit(db, "agent", "session.finish", session.id, {"status": status, "error": error})


def acknowledge(db, node, data):
    command = db.scalar(
        select(Command)
        .where(Command.id == data.get("command_id"), Command.node_id == node.id)
        .with_for_update()
    )
    if not command:
        # Terminal commands may have expired from server retention before the agent journal.
        # An authenticated stale ACK cannot mutate state and must not break reconciliation.
        return
    status = data.get("status")
    if status not in ("RUNNING", "STOPPED", "FAILED"):
        raise ValueError("Invalid command result")
    if command.ack_status in ("STOPPED", "FAILED"):
        return
    if status == "FAILED" and data.get("error"):
        node.last_error = str(data["error"])[:4000]
    session = db.get(Session, command.session_id) if command.session_id else None
    command.ack_status = status
    command.ack = {
        k: data[k]
        for k in ("status", "pid", "exit_code", "error", "resource_usage")
        if k in data
    }
    if session:
        if session.status in TERMINAL:
            return
        if isinstance(data.get("pid"), int):
            session.pid = data["pid"]
        session.resource_usage = data.get("resource_usage", {})
        if status == "RUNNING":
            if session.status == "STOPPING":
                return
            transition(session, "RUNNING")
            db.get(Job, session.job_id).status = "RUNNING"
        else:
            if command.operation == "stop" and status == "FAILED":
                session.error = str(data.get("error", "Stop failed"))
                transition(session, "UNKNOWN")
                return
            timed_out = session.error == "Execution timeout"
            finish(
                db,
                session,
                "TIMED_OUT" if timed_out else status,
                str(data.get("error"))[:4000] if data.get("error") else None,
                data.get("exit_code"),
            )
            # A terminal result also resolves any pending start/stop commands for this session.
            for pending in db.scalars(
                select(Command).where(
                    Command.session_id == session.id, Command.ack_status.is_(None)
                )
            ):
                pending.ack_status = status


def reconcile(db, node, processes):
    """Agent's durable journal resends terminal acks. Unknown absence never implies success."""
    known = {p.get("session_id") for p in processes if isinstance(p, dict)}
    for session in db.scalars(
        select(Session).where(Session.node_id == node.id, Session.status == "UNKNOWN")
    ):
        if session.id in known:
            transition(session, "RUNNING")
            job = db.get(Job, session.job_id)
            if job.status == "CANCELLED" or session.error == "Execution timeout":
                stop_session(db, session)
            else:
                job.status = "RUNNING"


def tick(db):
    now = utcnow()
    # One dispatch transaction across replicas, in addition to row leases/unique active-node index.
    if db.bind.dialect.name == "postgresql" and not db.scalar(
        text("SELECT pg_try_advisory_xact_lock(73294021)")
    ):
        return
    control = db.get(Control, 1)
    if not control:
        return
    from app.services.notifications import notify

    for node in db.scalars(select(Node).where(Node.status == "online")):
        if (
            node.revoked
            or not node.last_heartbeat
            or aware(node.last_heartbeat)
            < now - timedelta(seconds=settings().node_offline_seconds)
        ):
            node.status = "offline"
            notify(db, "node_offline", f"Node {node.hostname} offline", "warning")
            for s in db.scalars(
                select(Session).where(
                    Session.node_id == node.id, Session.status.in_(ACTIVE)
                )
            ):
                if s.status != "UNKNOWN":
                    transition(s, "UNKNOWN")
                j = db.get(Job, s.job_id)
                if j.status != "CANCELLED":
                    j.status = "UNKNOWN"
                    j.error = "Agent offline; awaiting process reconciliation"
    for s in db.scalars(
        select(Session).where(Session.status.in_(("STARTING", "RUNNING")))
    ):
        job = db.get(Job, s.job_id)
        if aware(s.started_at) + timedelta(seconds=job.timeout_seconds) < now:
            s.error = "Execution timeout"
            stop_session(db, s)
    for schedule in db.scalars(
        select(Schedule)
        .where(Schedule.enabled == True, Schedule.next_run <= now)
        .with_for_update(skip_locked=True)
    ):
        try:
            create_job(
                db,
                JobCreate.model_validate(schedule.job_template),
                f"schedule:{schedule.id}:{aware(schedule.next_run).isoformat()}",
                "scheduler",
            )
            # Avoid replay storms after downtime; execute once, then next future cadence.
            missed = (
                int(
                    (now - aware(schedule.next_run)).total_seconds()
                    // schedule.interval_seconds
                )
                + 1
            )
            schedule.next_run = aware(schedule.next_run) + timedelta(
                seconds=missed * schedule.interval_seconds
            )
        except (ValueError, HTTPException) as exc:
            schedule.enabled = False
            audit(
                db, "scheduler", "schedule.disabled", schedule.id, {"error": str(exc)}
            )
    if control.paused:
        return
    busy = set(db.scalars(select(Session.node_id).where(Session.status.in_(ACTIVE))))
    capacity = max(0, settings().scheduler_max_parallel - len(busy))
    if not capacity:
        return
    nodes = list(
        db.scalars(
            select(Node)
            .where(Node.status == "online", Node.revoked == False)
            .order_by(Node.id)
            .with_for_update(skip_locked=True)
        )
    )
    if control.policy == "round_robin" and control.last_node_id:
        nodes = [n for n in nodes if n.id > control.last_node_id] + [
            n for n in nodes if n.id <= control.last_node_id
        ]
    order = (
        (Job.priority.desc(), Job.created_at)
        if control.policy != "fifo"
        else (Job.created_at,)
    )
    jobs = list(
        db.scalars(
            select(Job)
            .where(Job.status == "QUEUED", Job.scheduled_at <= now)
            .order_by(*order)
            .limit(500)
            .with_for_update(skip_locked=True)
        )
    )
    for job in jobs:
        if capacity <= 0:
            break
        account = db.get(SteamAccount, job.account_id) if job.account_id else None
        if account and account.archived:
            job.status = "FAILED"
            job.error = "Account archived"
            continue
        assigned = job.node_id or (account.assigned_node_id if account else None)
        node = next(
            (
                n
                for n in nodes
                if n.id not in busy
                and (not assigned or n.id == assigned)
                and n.last_heartbeat
                and aware(n.last_heartbeat)
                > now - timedelta(seconds=settings().node_offline_seconds)
                and n.hardware.get("installed", {}).get(job.kind, job.kind == "test")
                and float(n.metrics.get("ram_available_gb", 0))
                >= job.payload.get("min_ram_gb", 0)
                and n.metrics.get("cpu_percent") is not None
                and float(n.metrics["cpu_percent"])
                <= job.payload.get("max_cpu_percent", 95)
            ),
            None,
        )
        if not node:
            continue
        if job.account_id and db.scalar(
            select(Session.id).where(
                Session.account_id == job.account_id, Session.status.in_(ACTIVE)
            )
        ):
            continue
        job.attempts += 1
        job.status = "STARTING"
        job.lease_owner = str(uuid.uuid4())
        job.lease_until = now + timedelta(seconds=job.timeout_seconds + 45)
        session = Session(
            job_id=job.id, node_id=node.id, account_id=job.account_id, status="STARTING"
        )
        db.add(session)
        db.flush()
        db.add(JobAttempt(job_id=job.id, session_id=session.id, attempt=job.attempts))
        db.add(
            Command(
                node_id=node.id,
                session_id=session.id,
                operation="start",
                payload={"kind": job.kind, "payload": job.payload},
                deadline=now + timedelta(seconds=job.timeout_seconds),
            )
        )
        audit(
            db,
            "scheduler",
            "job.dispatch",
            job.id,
            {"node_id": node.id, "session_id": session.id},
        )
        control.last_node_id = node.id
        busy.add(node.id)
        capacity -= 1
