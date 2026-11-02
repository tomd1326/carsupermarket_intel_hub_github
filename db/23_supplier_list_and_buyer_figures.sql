-- One supplier list and one set of buyer figures, shared by Assess Vehicle, the
-- Supplier Stock Evaluator and Cars Bought. utils/supplier_store.py and
-- utils/vehicle_store.py create these themselves the first time they are used;
-- this file is the same change for running by hand.

-- FALSE: we only get invoices from this supplier, so it is left out of the
-- evaluator's supplier choices.
ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS has_stock_list BOOLEAN NOT NULL DEFAULT TRUE;

-- The same supplier in Snowflake's contacts_suppliers__s. The id is what
-- identifies it, as a few names there are used twice.
ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS contact_id BIGINT, ADD COLUMN IF NOT EXISTS contact_name VARCHAR(250);

-- The text that identifies a supplier on its invoices.
CREATE TABLE IF NOT EXISTS supplier_invoice_names (
    id           SERIAL       PRIMARY KEY,
    invoice_text VARCHAR(100) NOT NULL,
    supplier_id  INTEGER      NOT NULL REFERENCES suppliers (supplier_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS supplier_invoice_names_text ON supplier_invoice_names (LOWER(invoice_text));

-- The buyer's current figures for a car. A purchase keeps its own copy at submit.
CREATE TABLE IF NOT EXISTS vehicle_buyer_figures (
    vrm           VARCHAR(20)   PRIMARY KEY REFERENCES vehicles (vrm),
    rrp           NUMERIC(12, 2),
    est_prep_cost NUMERIC(12, 2),
    updated_by    VARCHAR(100),
    updated_at    TIMESTAMP
);
