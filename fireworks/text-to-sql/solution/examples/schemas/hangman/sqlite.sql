CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, password_digest TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE (email));

CREATE TABLE phrases (id INTEGER PRIMARY KEY, body TEXT NOT NULL);

CREATE TABLE games (id INTEGER PRIMARY KEY, player_id INTEGER NOT NULL, phrase_id INTEGER NOT NULL, guess_limit INTEGER NOT NULL, completed_at TIMESTAMP, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (player_id) REFERENCES users (id), FOREIGN KEY (phrase_id) REFERENCES phrases (id), CHECK (guess_limit > 0));

CREATE TABLE turns (id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL, letter_guessed TEXT(1) NOT NULL, created_at TIMESTAMP NOT NULL, FOREIGN KEY (game_id) REFERENCES games (id), UNIQUE (game_id, letter_guessed));
