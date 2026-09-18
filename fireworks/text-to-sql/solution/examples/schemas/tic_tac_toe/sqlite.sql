CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, password_digest TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE (email));

CREATE TABLE games (id INTEGER PRIMARY KEY, winner_id INTEGER, completed_at TIMESTAMP, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (winner_id) REFERENCES users (id));

CREATE TABLE games_players (id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL, player_id INTEGER NOT NULL, token TEXT(1) NOT NULL, FOREIGN KEY (game_id) REFERENCES games (id), FOREIGN KEY (player_id) REFERENCES users (id), UNIQUE (game_id, token), UNIQUE (game_id, player_id));

CREATE TABLE turns (id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL, player_id INTEGER NOT NULL, position INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, FOREIGN KEY (game_id) REFERENCES games (id), FOREIGN KEY (player_id) REFERENCES users (id), CHECK (position BETWEEN 1 AND 9), UNIQUE (game_id, position), UNIQUE (game_id));
