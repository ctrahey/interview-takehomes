-- library schema: many-to-many (books<->authors), self-referencing (categories),
-- meaningful NULL semantics (loans.returned_date, members.membership_expiry).
-- SQLite dialect.

CREATE TABLE categories (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    parent_category_id    INTEGER REFERENCES categories(id)  -- nullable: NULL means top-level
);

CREATE TABLE authors (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    birth_year   INTEGER              -- nullable: unknown for a few historical/anonymous authors
);

CREATE TABLE books (
    id                 INTEGER PRIMARY KEY,
    title              TEXT NOT NULL,
    category_id        INTEGER NOT NULL REFERENCES categories(id),
    isbn               TEXT NOT NULL UNIQUE,
    publication_year   INTEGER NOT NULL,
    copies_owned       INTEGER NOT NULL
);

CREATE TABLE book_authors (
    book_id     INTEGER NOT NULL REFERENCES books(id),
    author_id   INTEGER NOT NULL REFERENCES authors(id),
    PRIMARY KEY (book_id, author_id)
);

CREATE TABLE members (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    email                TEXT NOT NULL,
    joined_date          TEXT NOT NULL,
    membership_expiry    TEXT          -- nullable: NULL means a lifetime membership, never expires
);

CREATE TABLE loans (
    id             INTEGER PRIMARY KEY,
    book_id        INTEGER NOT NULL REFERENCES books(id),
    member_id      INTEGER NOT NULL REFERENCES members(id),
    checkout_date  TEXT NOT NULL,
    due_date       TEXT NOT NULL,
    returned_date  TEXT               -- nullable: NULL means still checked out (possibly overdue)
);

CREATE INDEX idx_books_category ON books(category_id);
CREATE INDEX idx_book_authors_author ON book_authors(author_id);
CREATE INDEX idx_loans_book ON loans(book_id);
CREATE INDEX idx_loans_member ON loans(member_id);
