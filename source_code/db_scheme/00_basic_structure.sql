create table tasks
(
    task_id     int auto_increment,
    created     datetime                             null,
    last_update datetime                             null,
    status      enum ('created', 'pending', 'success', 'error') null,
    supplier_id    int                               null,
    filename    varchar(250)                         null,
    s3_location    varchar(250)                      null,
    constraint tasks_pk
        primary key (task_id)
);

create table suppliers
(
    supplier_id   int auto_increment,
    supplier_name varchar(250) null,
    constraint suppliers_pk
        primary key (supplier_id)
);

