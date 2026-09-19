# End-to-end evidence

Captured 2026-09-19 05:39 UTC against `accounts/fireworks/models/kimi-k2p7-code`.
Regenerate with `uv run python scripts/capture_evidence.py`.

Every step below is a live call. Timings are wall-clock.

## 1. Describe a domain in English, get DDL

> a climbing gym: members on membership plans, routes with difficulty grades, and check-ins recording when a member visited

```sql
CREATE TABLE membership_plans (plan_id INTEGER PRIMARY KEY AUTOINCREMENT, plan_name TEXT NOT NULL, monthly_fee REAL NOT NULL, duration_months INTEGER); CREATE TABLE members (member_id INTEGER PRIMARY KEY AUTOINCREMENT, first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, phone TEXT, date_joined DATE NOT NULL, plan_id INTEGER NOT NULL, FOREIGN KEY (plan_id) REFERENCES membership_plans(plan_id)); CREATE TABLE grades (grade_id INTEGER PRIMARY KEY AUTOINCREMENT, grade_code TEXT NOT NULL UNIQUE, grade_name TEXT, difficulty_rank INTEGER NOT NULL); CREATE TABLE routes (route_id INTEGER PRIMARY KEY AUTOINCREMENT, route_name TEXT NOT NULL, grade_id INTEGER NOT NULL, location TEXT, date_set DATE NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, setter_name TEXT, FOREIGN KEY (grade_id) REFERENCES grades(grade_id)); CREATE TABLE check_ins (check_in_id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL, check_in_time DATETIME NOT NULL, check_out_time DATETIME, FOREIGN KEY (member_id) REFERENCES members(member_id)); CREATE INDEX idx_check_ins_member ON check_ins(member_id); CREATE INDEX idx_check_ins_time ON check_ins(check_in_time); CREATE INDEX idx_routes_grade ON routes(grade_id);
```
_5579 ms._

## 2. Stand it up as a real database

Created 5 tables: `check_ins`, `grades`, `members`, `membership_plans`, `routes`.

This database has **no rows in it**, and that is the point of the next step: the query is checked against it anyway.

## 3. Every query is bind-checked against that database, empty or not

| probe | verdict | engine said |
| --- | --- | --- |
| a column that does not exist | **rejected** | `no such column: check_ins.definitely_not_a_column` |
| a table that does not exist | **rejected** | `no such table: members_typo` |
| a real query | accepted | `-` |

No sample data was required for any of that. This tier is always on, for every generated query, and it is what makes a syntactically-plausible-but-wrong query impossible to hand back.

## 4. Ask a question in English

> Which membership plan has the most check-ins? Show the plan name and the count.

```sql
SELECT mp.plan_name, COUNT(ci.check_in_id) AS check_in_count FROM membership_plans mp JOIN members m ON mp.plan_id = m.plan_id JOIN check_ins ci ON m.member_id = ci.member_id GROUP BY mp.plan_id, mp.plan_name ORDER BY check_in_count DESC LIMIT 1
```
_2409 ms, 1 attempt(s), validated by `ephemeral_sqlite`._

Returns the membership plan name with the highest number of member check-ins and that count.

