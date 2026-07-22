from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from clinic_voice.config import Settings
from clinic_voice.errors import DomainError
from clinic_voice.models import (
    Appointment,
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
    CatalogAppointmentType,
    CatalogBranch,
    CatalogPractitioner,
    ClinicCatalogResponse,
    SlotResult,
)
from clinic_voice.security import normalize_name


class AvailabilityService:
    def __init__(self, settings: Settings, pms: PmsAdapter, now_fn=None) -> None:
        self.settings = settings
        self.pms = pms
        self.now_fn = now_fn or (lambda: datetime.now(UTC))

    def catalog(self, db: Session) -> ClinicCatalogResponse:
        """Return the active, synchronized scheduling catalogue without internal IDs."""
        branches = db.scalars(
            select(Branch).where(Branch.active.is_(True)).order_by(Branch.display_name)
        ).all()
        practitioners = db.scalars(
            select(Practitioner)
            .where(Practitioner.active.is_(True))
            .order_by(Practitioner.display_name)
        ).all()
        appointment_types = db.scalars(
            select(AppointmentType)
            .where(AppointmentType.active.is_(True))
            .order_by(AppointmentType.display_name)
        ).all()
        active_branch_ids = {item.id for item in branches}
        active_type_ids = {item.id for item in appointment_types}
        branch_codes = {item.id: item.code for item in branches}
        type_codes = {item.id: item.code for item in appointment_types}
        practitioner_branch_codes: dict[str, list[str]] = {
            item.id: [] for item in practitioners
        }
        practitioner_type_codes: dict[str, list[str]] = {
            item.id: [] for item in practitioners
        }
        active_practitioner_ids = set(practitioner_branch_codes)
        for link in db.scalars(select(PractitionerBranch)).all():
            if (
                link.practitioner_id in active_practitioner_ids
                and link.branch_id in active_branch_ids
            ):
                practitioner_branch_codes[link.practitioner_id].append(
                    branch_codes[link.branch_id]
                )
        for link in db.scalars(select(PractitionerAppointmentType)).all():
            if (
                link.practitioner_id in active_practitioner_ids
                and link.appointment_type_id in active_type_ids
            ):
                practitioner_type_codes[link.practitioner_id].append(
                    type_codes[link.appointment_type_id]
                )
        specialties = sorted(
            {
                item.specialty
                for item in [*practitioners, *appointment_types]
                if item.specialty.strip()
            },
            key=str.casefold,
        )
        return ClinicCatalogResponse(
            specialties=specialties,
            branches=[CatalogBranch(code=item.code, name=item.display_name) for item in branches],
            practitioners=[
                CatalogPractitioner(
                    code=item.code,
                    name=item.spoken_name,
                    specialty=item.specialty,
                    branch_codes=sorted(practitioner_branch_codes[item.id]),
                    appointment_type_codes=sorted(practitioner_type_codes[item.id]),
                )
                for item in practitioners
            ],
            appointment_types=[
                CatalogAppointmentType(
                    code=item.code,
                    name=item.display_name,
                    specialty=item.specialty,
                )
                for item in appointment_types
            ],
            instruction=(
                "Use only this live catalogue. If the requested specialty, doctor, service, or "
                "branch is absent, say so before collecting scheduling constraints."
            ),
        )

    def search(self, db: Session, request: AvailabilityRequest) -> AvailabilityResponse:
        now = self.now_fn()
        local_now = now.astimezone(self.settings.timezone)
        date_from = request.date_from or local_now.date()
        date_to = request.date_to or date_from
        if date_from < local_now.date():
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
        all_branches = list(branches)
        all_practitioners = list(practitioners)
        all_appointment_types = list(appointment_types)
        if request.branch_code:
            branches = [
                item
                for item in branches
                if item.code.casefold() == request.branch_code.casefold()
            ]
            if not branches:
                return self._empty(
                    db,
                    request,
                    now,
                    "UNKNOWN_BRANCH",
                    "That branch is not in the active clinic catalogue.",
                    [item.display_name for item in all_branches],
                )
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
        if (request.practitioner_code or request.practitioner_name) and not practitioners:
            return self._empty(
                db,
                request,
                now,
                "UNKNOWN_PRACTITIONER",
                "That doctor is not in the active clinic catalogue.",
                [item.spoken_name for item in all_practitioners],
            )
        if request.specialty:
            specialty = normalize_name(request.specialty)
            specialty_practitioners = [
                item
                for item in all_practitioners
                if normalize_name(item.specialty) == specialty
            ]
            specialty_appointment_types = [
                item
                for item in all_appointment_types
                if normalize_name(item.specialty) == specialty
            ]
            supported_specialties = sorted(
                {
                    item.specialty
                    for item in [*all_practitioners, *all_appointment_types]
                    if item.specialty.strip()
                },
                key=str.casefold,
            )
            if not specialty_practitioners or not specialty_appointment_types:
                return self._empty(
                    db,
                    request,
                    now,
                    "UNSUPPORTED_SPECIALTY",
                    (
                        f"The clinic does not currently offer {request.specialty}. "
                        "State this immediately and offer a supported specialty or staff follow-up."
                    ),
                    supported_specialties,
                )
            specialty_practitioner_ids = {item.id for item in specialty_practitioners}
            specialty_type_ids = {item.id for item in specialty_appointment_types}
            practitioners = [
                item for item in practitioners if item.id in specialty_practitioner_ids
            ]
            appointment_types = [
                item for item in appointment_types if item.id in specialty_type_ids
            ]
        if request.appointment_type_code:
            globally_matching_types = [
                item
                for item in all_appointment_types
                if item.code.casefold() == request.appointment_type_code.casefold()
            ]
            appointment_types = [
                item
                for item in appointment_types
                if item.code.casefold() == request.appointment_type_code.casefold()
            ]
        elif request.appointment_type_name:
            globally_matching_types = self._match_named(
                all_appointment_types,
                request.appointment_type_name,
                lambda item: (item.display_name,),
            )
            appointment_types = self._match_named(
                appointment_types,
                request.appointment_type_name,
                lambda item: (item.display_name,),
            )
        else:
            globally_matching_types = all_appointment_types
        if (
            request.appointment_type_code or request.appointment_type_name
        ) and not globally_matching_types:
            return self._empty(
                db,
                request,
                now,
                "UNKNOWN_APPOINTMENT_TYPE",
                "That appointment type is not in the active clinic catalogue.",
                [item.display_name for item in all_appointment_types],
            )

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

        if not eligible_pairs:
            return self._empty(
                db,
                request,
                now,
                "INELIGIBLE_COMBINATION",
                (
                    "Those clinic items exist, but that doctor, service, and branch combination "
                    "is not configured. Ask which preference may be changed."
                ),
            )

        def load(pair):
            branch, practitioner, appointment_type = pair
            slots = self.pms.available_times(
                db,
                branch,
                practitioner,
                appointment_type,
                date_from,
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

        # Cliniko availability is queried under a business/branch. A practitioner attached to more
        # than one business can therefore be returned as free at branch B even though this service
        # already booked them at branch A. Apply the service's cross-branch source of truth before
        # offering slots; the booking-time overlap check remains as the final race-condition guard.
        if candidates:
            window_start = datetime.combine(date_from, time.min, self.settings.timezone).astimezone(
                UTC
            )
            window_end = datetime.combine(
                date_to + timedelta(days=1), time.min, self.settings.timezone
            ).astimezone(UTC)
            practitioner_ids = {practitioner.id for _, _, practitioner, _ in candidates}
            busy = db.scalars(
                select(Appointment).where(
                    Appointment.practitioner_id.in_(practitioner_ids),
                    Appointment.status.in_(("pending_sync", "confirmed")),
                    Appointment.starts_at < window_end,
                    Appointment.ends_at > window_start,
                )
            ).all()
            candidates = [
                candidate
                for candidate in candidates
                if not self._overlaps_local_appointment(candidate[0], candidate[2].id, busy)
            ]

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
                code="NO_AVAILABLE_SLOTS",
                search_id=search.id,
                searched_at=now,
                slots=[],
                instruction="No live slots match. Ask which constraint can be widened, then search again.",
            )
        return AvailabilityResponse(
            status="available",
            code="OK",
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

    @classmethod
    def _overlaps_local_appointment(
        cls, slot: PmsSlot, practitioner_id: str, appointments: list[Appointment]
    ) -> bool:
        slot_start = cls._aware(slot.starts_at)
        slot_end = cls._aware(slot.ends_at)
        return any(
            item.practitioner_id == practitioner_id
            and cls._aware(item.starts_at) < slot_end
            and cls._aware(item.ends_at) > slot_start
            for item in appointments
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    def _empty(
        self,
        db: Session,
        request: AvailabilityRequest,
        now: datetime,
        code: str,
        instruction: str,
        suggestions: list[str] | None = None,
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
            code=code,
            search_id=search.id,
            searched_at=now,
            slots=[],
            suggestions=suggestions or [],
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
