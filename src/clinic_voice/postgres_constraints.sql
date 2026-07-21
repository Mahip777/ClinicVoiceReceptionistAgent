CREATE EXTENSION IF NOT EXISTS btree_gist;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'appointments_no_practitioner_overlap'
    ) THEN
        ALTER TABLE appointments
        ADD CONSTRAINT appointments_no_practitioner_overlap
        EXCLUDE USING gist (
            practitioner_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        )
        WHERE (status IN ('pending_sync', 'confirmed'));
    END IF;
END $$;

