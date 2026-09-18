# reddit — ingestion notes

A Reddit-like link aggregator: submissions, voting, and nested commenting.

## D6 pipeline

`reddit.postgres.sql` (vendored, verbatim) -> `foundation.ddl.parse_ddl` -> `graph.json` (dialect-neutral entity graph) -> `foundation.ddl.render_ddl` -> `sqlite.sql` (executed by D9's sample-database lifecycle).

## parse_ddl warnings

- column 'users'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- column 'submissions'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON submissions(author_id)
- column 'submission_votes'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON submission_votes(voter_id)
- column 'comments'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON comments(author_id)
- skipped non-CREATE-TABLE statement: CREATE INDEX ON comments(submission_id)
- skipped non-CREATE-TABLE statement: CREATE INDEX ON comments(parent_id)
- column 'comment_votes'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON comment_votes(voter_id)

## row counts (seed.sql)

- users: 30
- submissions: 40
- submission_votes: 73
- comments: 92
- comment_votes: 73
