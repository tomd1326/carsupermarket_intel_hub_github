CREATE TABLE IF NOT EXISTS users (
    username     VARCHAR(100) PRIMARY KEY,
    pwd_hash     VARCHAR(64)  NOT NULL
);
