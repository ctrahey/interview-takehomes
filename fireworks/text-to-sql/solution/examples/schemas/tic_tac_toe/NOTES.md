# tic_tac_toe — ingestion notes

A collection of tic-tac-toe games: users, games, and turns.

## D6 pipeline

`tic_tac_toe.postgres.sql` (vendored, verbatim) -> `foundation.ddl.parse_ddl` -> `graph.json` (dialect-neutral entity graph) -> `foundation.ddl.render_ddl` -> `sqlite.sql` (executed by D9's sample-database lifecycle).

## parse_ddl warnings

- column 'users'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- column 'games'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON games(winner_id)
- column 'games_players'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON games_players(player_id)
- column 'turns'.'id': Postgres SERIAL is an identity/autoincrement pseudo-type, not modeled -- captured as 'INT'; the sequence/autoincrement behavior itself is dropped
- skipped non-CREATE-TABLE statement: CREATE INDEX ON turns(game_id, player_id)
- skipped non-CREATE-TABLE statement: CREATE INDEX ON turns(player_id)

## row counts (seed.sql)

- users: 24
- games: 50
- games_players: 100
- turns: 40
