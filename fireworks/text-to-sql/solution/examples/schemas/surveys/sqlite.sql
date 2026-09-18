CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, password_digest TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, UNIQUE (email));

CREATE TABLE surveys (id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL, title TEXT NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (author_id) REFERENCES users (id));

CREATE TABLE survey_choices (id INTEGER PRIMARY KEY, survey_id INTEGER NOT NULL, description TEXT NOT NULL, FOREIGN KEY (survey_id) REFERENCES surveys (id));

CREATE TABLE survey_responses (id INTEGER PRIMARY KEY, survey_id INTEGER NOT NULL, respondent_id INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (survey_id) REFERENCES surveys (id), FOREIGN KEY (respondent_id) REFERENCES users (id), UNIQUE (survey_id, respondent_id));

CREATE TABLE survey_response_choices (id INTEGER PRIMARY KEY, survey_choice_id INTEGER NOT NULL, survey_response_id INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, FOREIGN KEY (survey_choice_id) REFERENCES survey_choices (id), FOREIGN KEY (survey_response_id) REFERENCES survey_responses (id), UNIQUE (survey_choice_id, survey_response_id));
