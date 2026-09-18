# photo_gallery — ingestion notes

A basic photo gallery: users create albums and upload photos to them.

## D6 pipeline

`photo_gallery.postgres.sql` (vendored, verbatim) -> `foundation.ddl.parse_ddl` -> `graph.json` (dialect-neutral entity graph) -> `foundation.ddl.render_ddl` -> `sqlite.sql` (executed by D9's sample-database lifecycle).

## parse_ddl warnings

- column 'users'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- column 'albums'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON albums(user_id)
- column 'photos'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON photos(album_id)

## row counts (seed.sql)

- users: 20
- albums: 30
- photos: 87
