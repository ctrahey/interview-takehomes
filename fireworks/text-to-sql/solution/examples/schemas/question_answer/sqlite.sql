CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, username TEXT NOT NULL, password_digest TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE (email), UNIQUE (username));

CREATE TABLE answers (id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL, question_id INTEGER NOT NULL, body TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (author_id) REFERENCES users (id), FOREIGN KEY (question_id) REFERENCES questions (id), UNIQUE (author_id, question_id));

CREATE TABLE questions (id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL, best_answer_id INTEGER, title TEXT NOT NULL, body TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (author_id) REFERENCES users (id), FOREIGN KEY (best_answer_id) REFERENCES answers (id));

CREATE TABLE answer_votes (id INTEGER PRIMARY KEY, answer_id INTEGER NOT NULL, voter_id INTEGER NOT NULL, score INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (answer_id) REFERENCES answers (id), FOREIGN KEY (voter_id) REFERENCES users (id), CHECK (score IN (-1, 1)), UNIQUE (answer_id, voter_id));
