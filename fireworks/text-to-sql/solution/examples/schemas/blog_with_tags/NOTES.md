# blog_with_tags — ingestion notes

A basic blog where users publish articles and tag them with arbitrary tags.

## D6 pipeline

`blog_with_tags.postgres.sql` (vendored, verbatim) -> `foundation.ddl.parse_ddl` -> `graph.json` (dialect-neutral entity graph) -> `foundation.ddl.render_ddl` -> `sqlite.sql` (executed by D9's sample-database lifecycle).

## parse_ddl warnings

- column 'users'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- column 'articles'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON articles(author_id)
- column 'tags'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- column 'taggings'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON taggings(tag_id)

## row counts (seed.sql)

- users: 25
- articles: 40
- tags: 20
- taggings: 57
