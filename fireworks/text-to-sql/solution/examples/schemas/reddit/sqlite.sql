CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, username TEXT NOT NULL, password_digest TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE (username), UNIQUE (email));

CREATE TABLE submissions (id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL, url TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (author_id) REFERENCES users (id));

CREATE TABLE submission_votes (id INTEGER PRIMARY KEY, submission_id INTEGER NOT NULL, voter_id INTEGER NOT NULL, score INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (submission_id) REFERENCES submissions (id), FOREIGN KEY (voter_id) REFERENCES users (id), CHECK (SCORE IN (-1, 1)), UNIQUE (submission_id, voter_id));

CREATE TABLE comments (id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL, submission_id INTEGER NOT NULL, parent_id INTEGER, body TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (author_id) REFERENCES users (id), FOREIGN KEY (submission_id) REFERENCES submissions (id), FOREIGN KEY (parent_id) REFERENCES comments (id));

CREATE TABLE comment_votes (id INTEGER PRIMARY KEY, comment_id INTEGER NOT NULL, voter_id INTEGER NOT NULL, score INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (comment_id) REFERENCES comments (id), FOREIGN KEY (voter_id) REFERENCES users (id), CHECK (score IN (-1, 1)), UNIQUE (comment_id, voter_id));
