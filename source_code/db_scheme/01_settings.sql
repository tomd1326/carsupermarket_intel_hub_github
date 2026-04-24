CREATE TABLE settings (
    key   varchar(100) PRIMARY KEY,
    value varchar(250) NOT NULL
);

INSERT INTO settings (key, value) VALUES
    ('number_to_buy',  '200'),
    ('max_days_sales', '90'),
    ('max_dts',        '45'),
    ('min_gpu_m',      '0'),
    ('cap_workers',    '25'),
    ('cap_rate_limit', '100');
