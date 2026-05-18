CREATE TYPE task_status AS ENUM ('created', 'pending', 'success', 'error');

CREATE TABLE tasks (
    task_id          SERIAL PRIMARY KEY,
    created          timestamp without time zone NULL,
    last_update      timestamp without time zone NULL,
    status           task_status NULL,
    message          varchar(250) NULL,
    supplier_id      integer NULL,
    filename         varchar(250) NULL,
    s3_location      varchar(250) NULL,
    file_fingerprint varchar(100) NULL,
    total_rows       integer NULL,
    execution_time   float NULL
    -- Optionally add FKs if desired:
    -- ,FOREIGN KEY (supplier_id) REFERENCES suppliers(supplier_id)
);


CREATE TABLE suppliers (
    supplier_id   SERIAL PRIMARY KEY,
    supplier_name varchar(250)
);