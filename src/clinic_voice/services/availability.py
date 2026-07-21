from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from clinic_voice.config import Settings
from clinic_voice.errors import DomainError
from clinic_voice.models import (
    AppointmentType,
    AvailabilitySearch,
    Branch,
    OfferedSlot,
    Practitioner,
    PractitionerAppointmentType,
    PractitionerBranch,
)
from clinic_voice.pms import PmsAdapter, PmsSlot
from clinic_voice.schemas import (
    AvailabilityRequest,
    AvailabilityResponse,
    SlotResult,
)
from clinic_voice.security import normalize_name


class AvailabilityService:
    def __init__(self, settings: Settings, pms: PmsAdapter, now_fn=None) -> None:
        self.settings = settings
        self.pms = pms
        self.now_fn = now_fn or (lambda: datetime.now(UTC))

    def search(self, db: Session, request: AvailabilityRequest) -> AvailabilityResponse:
        now = self.now_fn()
        local_now = now.astimezone(self.settings.timezone)
        date_to = request.date_to or request.date_from
        if request.date_from < local_now.date():
            raise DomainError("DATE_IN_PAST", "Availability cannot be searched in the past.")
        max_date = local_now.date() + timedelta(days=self.settings.search_horizon_days)
        if date_to > max_date:
            raise DomainError(
                "SEARCH_HORIZON_EXCEEDED", f"Search only through {max_date.isoformat()}."
            )

        branches = db.scalars(select(Branch).where(Branch.active.is_(True))).all()
        practitioners = db.scalars(select(Practitioner).where(Practitioner.active.is_(True))).all()
        appointment_types = db.scalars(
            select(AppointmentType).where(AppointmentType.active.is_(True))
        ).all()
        if request.branch_code:
            branches = [item for item in branches if item.code == request.branch_code]
        if request.practitioner_code:
            practitioners = [
                item
                for item in practitioners
                if item.code.casefold() == request.practitioner_code.casefold()
            ]
        elif request.practitioner_name:
            practitioners = self._match_named(
                practitioners,
                request.practitioner_name,
                lambda item: (item.display_name, item.spoken_name),
            )
        if request.specialty:
            specialty = request.specialty.casefold()
            practitioners = [
                item for item in practitioners if item.specialty.casefold() == specialty
            ]
            appointment_types = [
                item for item in appointment_types if item.specialty.casefold() == specialty
            ]
        if request.appointment_type_code:
            appointment_types = [
                item
                for item in appointment_types
                if item.code.casefold() == request.appointment_type_code.casefold()
            ]
        elif request.appointment_type_name:
            appointment_types = self._match_named(
                appointment_types,
                request.appointment_type_name,
                lambda item: (item.display_name,),
            )
        if not branches or not practitioners or not appointment_types:
            return self._empty(db, request, now, "No matching branch, doctor, or service exists.")

        branch_links = {
            (item.practitioner_id, item.branch_id)
            for item in db.scalars(select(PractitionerBranch)).all()
        }
        type_links = {
            (item.practitioner_id, item.appointment_type_id)
            for item in db.scalars(select(PractitionerAppointmentType)).all()
        }
        eligible_pairs: list[tuple[Branch, Practitioner, AppointmentType]] = []
        for branch in branches:
            for practitioner in practitioners:
                if (practitioner.id, branch.id) not in branch_links:
                    continue
                for appointment_type in appointment_types:
                    if (practitioner.id, appointment_type.id) not in type_links:
                        continue
                    eligible_pairs.append((branch, practitioner, appointment_type))

        def load(pair):
            branch, practitioner, appointment_type = pair
            slots = self.pms.available_times(
                db,
                branch,
                practitioner,
                appointment_type,
                request.date_from,
                date_to,
            )
            return pair, slots

        if self.settings.pms_provider == "cliniko" and len(eligible_pairs) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(eligible_pairs))) as executor:
                loaded = list(executor.map(load, eligible_pairs))
        else:
            loaded = [load(pair) for pair in eligible_pairs]

        candidates: list[tuple[PmsSlot, Branch, Practitioner, AppointmentType]] = []
        for (branch, practitioner, appointment_type), slots in loaded:
            for slot in slots:
                if self._matches(slot, request, now):
                    candidates.append((slot, branch, practitioner, appointment_type))

        candidates.sort(key=lambda item: item[0].starts_at)
        candidates = candidates[: request.limit]
        search = AvailabilitySearch(
            call_id=request.call_id,
            constraints=request.model_dump(mode="json"),
            fresh_at=now,
        )
        db.add(search)
        db.flush()
        results: list[SlotResult] = []
        for slot, branch, practitioner, appointment_type in candidates:
            offered = OfferedSlot(
                search_id=search.id,
                branch_id=branch.id,
                practitioner_id=practitioner.id,
                appointment_type_id=appointment_type.id,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                expires_at=now + timedelta(seconds=self.settings.offer_ttl_seconds),
                source_payload=slot.source_payload,
            )
            db.add(offered)
            db.flush()
            results.append(
                SlotResult(
                    offer_id=offered.id,
                    branch_code=branch.code,
                    branch_name=branch.display_name,
                    practitioner_code=practitioner.code,
                    practitioner_name=practitioner.spoken_name,
                    appointment_type_code=appointment_type.code,
                    appointment_type_name=appointment_type.display_name,
                    starts_at=slot.starts_at,
                    ends_at=slot.ends_at,
                    patient_duration_minutes=appointment_type.patient_duration_minutes,
                    price_minor=appointment_type.price_minor,
                    currency=appointment_type.currency,
                )
            )
        db.commit()
        if not results:
            return AvailabilityResponse(
                status="unavailable",
                search_id=search.id,
                searched_at=now,
                slots=[],
                instruction="No live slots match. Ask which constraint can be widened, then search again.",
            )
        return AvailabilityResponse(
            status="available",
            search_id=search.id,
            searched_at=now,
            slots=results,
            instruction=(
                "Offer these live results. Speak the branch paired with each offer_id exactly as returned."
            ),
        )

    def is_still_available(self, db: Session, offered: OfferedSlot) -> bool:
        branch = db.get(Branch, offered.branch_id)
        practitioner = db.get(Practitioner, offered.practitioner_id)
        appointment_type = db.get(AppointmentType, offered.appointment_type_id)
        local_date = offered.starts_at.astimezone(self.settings.timezone).date()
        slots = self.pms.available_times(
            db, branch, practitioner, appointment_type, local_date, local_date
        )
        return any(slot.starts_at == offered.starts_at for slot in slots)

    def alternatives_for_offer(self, db: Session, offered: OfferedSlot) -> list[SlotResult]:
        search = db.get(AvailabilitySearch, offered.search_id)
        if not search:
            return []
        request = AvailabilityRequest.model_validate(search.constraints)
        return self.search(db, request).slots

    def _matches(self, slot: PmsSlot, request: AvailabilityRequest, now: datetime) -> bool:
        local = slot.starts_at.astimezone(self.settings.timezone)
        if request.preferred_weekdays and local.weekday() not in request.preferred_weekdays:
            return False
        if request.time_window:
            local_time = local.time().replace(tzinfo=None)
            if not (request.time_window.start <= local_time <= request.time_window.end):
                return False
        if request.around_time:
            target = datetime.combine(local.date(), request.around_time, self.settings.timezone)
            if abs((local - target).total_seconds()) > request.around_minutes * 60:
                return False
        if local.date() == now.astimezone(self.settings.timezone).date():
            if slot.starts_at < now + timedelta(minutes=self.settings.same_day_lead_minutes):
                return False
        return True

    def _empty(
        self, db: Session, request: AvailabilityRequest, now: datetime, instruction: str
    ) -> AvailabilityResponse:
        search = AvailabilitySearch(
            call_id=request.call_id,
            constraints=request.model_dump(mode="json"),
            fresh_at=now,
        )
        db.add(search)
        db.commit()
        return AvailabilityResponse(
            status="unavailable",
            search_id=search.id,
            searched_at=now,
            slots=[],
            instruction=instruction,
        )

    @staticmethod
    def _match_named(items, requested_name: str, names) -> list:
        """Accept an exact live name or an unambiguous partial name such as 'Nisha'."""
        honorifics = {"dr", "doctor", "mr", "mrs", "ms", "miss"}

        def tokens(value: str) -> set[str]:
            return {
                token
                for token in normalize_name(value).replace(".", " ").split()
                if token not in honorifics
            }

        requested = tokens(requested_name)
        exact = [
            item
            for item in items
            if any(tokens(candidate) == requested for candidate in names(item))
        ]
        if exact:
            return exact
        partial = [
            item
            for item in items
            if requested and any(requested <= tokens(candidate) for candidate in names(item))
        ]
        return partial if len(partial) == 1 else []
