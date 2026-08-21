-- CAP model images, one row per CAP id + registration year.
--
-- Images belong to the CAP id, not the VRM — every C250d AMG Line shares CAP id
-- 69530 and therefore one picture — so storing per VRM would refetch the same
-- image for every car of that model.
--
-- The registration year is part of the key because CAP's image API picks the
-- model year current at the registration date, so one CAP id can have different
-- images either side of a facelift.
--
-- not_available records a CAP id CAP has no image for, so a vehicle with no
-- picture is asked about once rather than on every popup open.
CREATE TABLE IF NOT EXISTS cap_images (
    cap_id              INTEGER         NOT NULL,
    registration_year   SMALLINT        NOT NULL,
    image               BYTEA,
    content_type        VARCHAR(50),
    width               SMALLINT,
    height              SMALLINT,
    not_available       BOOLEAN         NOT NULL DEFAULT FALSE,
    fetched_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cap_id, registration_year)
);
