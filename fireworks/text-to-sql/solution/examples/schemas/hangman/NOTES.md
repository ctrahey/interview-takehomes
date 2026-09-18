# hangman — ingestion notes

A player-vs-computer Hangman game: users, games, turns, and phrases.

## D6 pipeline

`hangman.postgres.sql` (vendored, verbatim) -> `foundation.ddl.parse_ddl` -> `graph.json` (dialect-neutral entity graph) -> `foundation.ddl.render_ddl` -> `sqlite.sql` (executed by D9's sample-database lifecycle).

## parse_ddl warnings

- column 'users'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- column 'phrases'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- column 'games'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON games(player_id)
- column 'turns'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped

## row counts (seed.sql)

- users: 22
- phrases: 21
- games: 40
- turns: 75
