from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from clinic_voice.models import CallSession, FollowupRequest
from clinic_voice.schemas import CheckpointRequest, FollowupRequestSchema
from clinic_voice.security import normalize_phone


class CallService:
    def checkpoint(self, db: Session, request: CheckpointRequest) -> CallSession:
        session = db.scalar(select(CallSession).where(CallSession.call_id == request.call_id))
        if not session:
            session = CallSession(
                call_id=request.call_id,
                phone_e164=normalize_phone(request.phone_e164),
                direction="inbound",
                status="active",
            )
            db.add(session)
        session.patient_id = request.patient_id or session.patient_id
        session.intent = request.intent or session.intent
        session.checkpoint = {**(session.checkpoint or {}), **request.state}
        session.status = "active"
        db.commit()
        return session

    def mark_ended(
        self,
        db: Session,
        call_id: str,
        reason: str | None,
        transcript: str | None,
    ) -> None:
        session = db.scalar(select(CallSession).where(CallSession.call_id == call_id))
        if not session:
            return
        normal_reasons = {"user_hangup", "agent_hangup", "completed", "ended"}
        session.status = "completed" if reason in normal_reasons else "disconnected"
        session.ended_reason = reason
        session.transcript = transcript
        db.commit()

    def create_followup(self, db: Session, request: FollowupRequestSchema) -> FollowupRequest:
        item = FollowupRequest(
            call_id=request.call_id,
            patient_id=request.patient_id,
            phone_e164=normalize_phone(request.phone_e164),
            reason=request.reason,
            details=request.details,
            priority=request.priority,
        )
        db.add(item)
        db.commit()
        return item
