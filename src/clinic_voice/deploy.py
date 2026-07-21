from __future__ import annotations

from sqlalchemy import exists, select

from clinic_voice.config import get_settings
from clinic_voice.database import SessionLocal, create_schema
from clinic_voice.models import AppointmentType, Branch, Practitioner
from clinic_voice.sync_cliniko import ClinikoSync


def cliniko_catalog_ready() -> bool:
    with SessionLocal() as db:
        checks = [
            db.scalar(
                select(
                    exists().where(Branch.active.is_(True), Branch.external_id.is_not(None))
                )
            ),
            db.scalar(
                select(
                    exists().where(
                        Practitioner.active.is_(True), Practitioner.external_id.is_not(None)
                    )
                )
            ),
            db.scalar(
                select(
                    exists().where(
                        AppointmentType.active.is_(True),
                        AppointmentType.external_id.is_not(None),
                    )
                )
            ),
        ]
        return all(checks)


def main() -> None:
    """Initialize persistent deployment state without relying on paid deploy hooks."""
    settings = get_settings()
    create_schema()
    if settings.pms_provider != "cliniko":
        print("Deployment bootstrap: schema ready; Cliniko sync not requested.")
        return
    if cliniko_catalog_ready():
        print("Deployment bootstrap: existing Cliniko catalogue is ready; sync skipped.")
        return
    print("Deployment bootstrap: Cliniko catalogue is empty; synchronizing metadata.")
    ClinikoSync().run(include_patients=False)


if __name__ == "__main__":
    main()
