CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, password_digest TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE (email));

CREATE TABLE articles (id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (author_id) REFERENCES users (id));

CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE (name));

CREATE TABLE taggings (id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (article_id) REFERENCES articles (id), FOREIGN KEY (tag_id) REFERENCES tags (id), UNIQUE (article_id, tag_id));
