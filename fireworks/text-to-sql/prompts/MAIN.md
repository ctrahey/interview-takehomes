# A Text-to-SQL Utility

There are two major modes of the problem domain. Naturally, a text-to-sql solution should help me produce valid queries (DML). Perhaps this is the only mode that we'll productize. But there is strategic value in the other phase: Creating schemas (DDL). This will open up new utility for customers, but also will help us validate the query generation for unique scenarios more naturally.
At the inner core, these can use the same LLM-assisted functionality - the marquee "Text to SQL" is part of both modes. But the applicaiton layer behaves differently. When generating schema, we'll want features that can create running workloads to help the user realize value from their new schema (as well as validate it). The query side shuold be usable even if the database either doesn't exist or is running externally. 

There will likewise be strategic value in a few supporting capabilities:

1. Generating and loading data fixtures, either conformant or intentionally maligned
2. Generating and running test scenarios
3. Evaluation and "self awareness" (optimizition)

# The Experience

Humans will use this solution through various "front ends" like chat interfaces, voice-to-text, or custom solutions.
We'll center the product on a simple HTTP API, to decouple the core capability from the individual client applicaitons.
However, thinking through a couple of these and building out at least 2 reference implementations will greatly improve our approach to the solution and our ability to validate it. 

These are mini-projects in themselves, and are therefore more fully described in sub-folders here. We plan them first only because use-cases like these should drive our API design. They should only be built first if they prove helpful to the development & validation of the API. To be maximally clear: Our ACTUAL product is the API that performs text-to-SQL and these experience products will _depend upon the API_. 

## Slack Bot
The user will add a chatbot to a channel or DM with it. They'll ask for queries or schemas, and the bot will deliver!

## Website for Schema Design
The user visits a website and begins chatting about a domain of interest. The site will use our API to iteratively produce DDL and test data, showing the user the progress all the way.

## The HTTP Client (implied - a Behavioral Test Suite)
We will also define use cases in more direct HTTP terms within a behavioral test suite that is "aware of" the HTTP contract.

# The Domain Model
To provide value, we'll build a real applicaiton layer that performs more than just prompt injection. We'll store state for users that will help them iterate without relying on our client applications to manage the session context.

## Domain Objects

* Project: container that holds related models and session history so a user can manage their work
* Session: Logically like a conversation - has a durable scope but may include several models and queries. We don't need to model out what "scope" means, but simply emphasize here that sometimes users will want to be referring to multiple models - for example when working on data migration or design scenarios. Sessions are scoped to projects.
* Data Model: This is the logical underpinning behind the "schema of interest" or "schema under discussion"... A session/conversation MAY refer to any number of models (zero-or-more). Models are versioned and optionally named.
* Schema: A concrete set of DDL statements. This is a concrete "projection" of a data model. 
* Query: the SQL, usually an output of our system
* Data: importable "sample data"
* Database: a running instance of an RDBMS like SQLite or Postgres
* Database Engine: Determines the ruleset for what valid SQL is. Postgres, MySQL, etc..
* Question: A natural-language string that is to be interpreted into a query. 

Clarifications: 

1. Data models are "purist" biased - always strongly normalized and rigorous, ontologically grounded. Schemas are derivitive and may intentionally include pragmatic factors like denormalization, encodings, etc
2. To simplify the API design, there will always be a default "project" and "session" for every user, so they are optional parameters and not necassarily part of the URL schema.
3. Use UUID by default in API parameterization (especially path segments)

## Behaviors

### Hero Story 1: Questions-to-Queries
I can send a schema as plain text in a standard format (like DDL SQL or DBML) along with a natural-language question, and I recieve back a SQL string that would satisfy this question against a database that has this schema. 

### Hero Story 2: Develop a Schema
I can send a natural language description of a domain model, and the system will return to me some DDL SQL, along with a UUID handle I can use to refer to this model in the future so I can iterate and use downstream features against it.


# Solution Design
The HTTP interface will hand operations off to an application layer that is interface agnostic (so we can develop a CLI version, for example). The application layer is fundamentally a context/memory/prompt managment system that uses the Fireworks.ai platform for inference at various moments. Due to the nondeterministic nature of natural language, we will build a three-layer solution: The first is a fully deterministic CRUD-style layer where the system state is accessible. The second is a semi-deterministic layer (partially structured inputs, but including a small "question" natural-language prompt input) that is aware of the actions/verbs/domain-objects, where the natural language portion is curated to be narrowly a description of the "question that will be converted to a query", but otherwise the request includes strutured inputs like the schema, etc. The third layer is more purely natural-language focused and could include much larger prompts that require interpretation first to determine if the prompt includes reference to a saved schema, describes a schema in natural language, or is some more general question (like asking for help about the system). These layers build on top of each other.

## The Deterministic API
Normal CRUD operations against our domain objects. List my saved models, my databases, sessions, etc. This layer should be rock-solid, fully determninistic, includes ZERO natural language features.
The most interesting part of this API is the inclusion of sample databases and related features. Users can create, load, query, and destroy sample databases. The "engine" should be a choice, but the list of available choices will be limited to SQLite for initial release (the design should be pluggable of course)

## The semi-deterministic API
Requests include a prompt/question input that is expected to be natural-language, but where the interpretation of that input will be heavily constrained to "this speach-act can be converted to SQL, or else there is an error". It is necessary then that this API includes enough structured context to support such conversion. In order to encourage loose coupling, this layer should be designed to be stateless first; meaning each request should include the full plain-text of any supporting schemas or models as part of the request payload and it should be possible to satisfy the request directly. Later, a convenience vaneer can be added where such schemas/models can be referred to by pointer/name. But at its core, this layer should rely on other parts of the system to assemble valid and stateless requests.

## The Fully Natural-Language API
This layer can be thought of as a natural-language orchestrator of the lower layers and is meant to provide the human user with a maximally helpful interface to the system. The first thing this layer needs to do is classify the input speach-act to understand what the user is looking to accomplish or wants the system to do. This could be several things, and so it is possible this layer needs some lightweight notions of workflows, tasks, etc... but let's keep it light at first.

Classifications: 
1. Help about the system itself
2. Queries about the user's domain-objects in this system, like "remind me what the latest model we were discussing is?"
3. Creation of schemas/models
4. Conversion of a natural-language question into SQL in the context of a schema/model that could be described herein or referenced by name from saved models
5. Load some sample data to a sample database
6. execute queries against a pre-loaded database

# System Design

The deterministic layer can be built as a classic stateful backend application, but with clean seperation of application and HTTP interface. The interface layer will be Python (FastAPI). The application layer will also be python with an RDBMS adapter (like Alchemy) with SQLite default.

The two layers that include natural-language features will include more modern LLM-savvy patterns. Most notably, they need to manage prompts/prompt-templates as part of the solution. There are also more semantically non-deterministic state-machines to build out, with appreciation that these will likely be a point of design iteration and maintanence.

## Components

There are three "Applications" that should be shipped as Python packages with import-and-function-call interfaces, and then an HTTP server that presents a thin layer atop. There is also a CLI Application

### Foundation
A Python program that offers function-call interfaces and performs the data management. Owns 100% of all persistence. Default to SQLAlchemy with SQLite.
Models SQL-adjacent concepts but also the concerns related to user interactions like sessions, questions, etc.
This layer WILL have data models for natural-language items, but it should NOT involve any LLM usage for interpreting those strings (that's what the other layers are for!)

### Text-to-SQL
Built to be completely stateless (there should be ZERO dependency on the Foundation application code!!), this application will use the Fireworks.ai platform to do LLM inference to convert textual inputs into SQL outputs.

### Natural Language
The "orchestrator" layer presents a clean and simple interface and hides as much of the housekeeping as possible. This application has dependencies on BOTH the Foundation and Text-to-SQL applications. The primary use case is using the foundation layer to do context and memory management, in order to craft effective inputs for calling the text-to-sql application.

Crucially - this MAY include manipulating the actual-human prompts for clarity, or to break it down into smaller steps. 

This layer may have lightweight multi-step workflows, etc (Example: creating a model, loading a sample database with data, deriving a query, then executing it, and summarizing the results)

This layer will HEAVILY utilize the fireworks.ai platform at many vital moments as it works from incoming prompt through a plan of action and developing a response. 

This layer is almost exclusively natural-language in and out, but there should be an affordance for outputting artifacts like .sql or .json files.

### The HTTP Application
A relatively lightweight HTTP server that encapsulates all web/http concerns and presents an OpenAPI spec (with FastAPI) that users can call.

### The CLI Applicaiton
A "click" based python application that makes it easy to try all features locally.

## Example Prompts:

It is early in the design phase as I write this, so take these as guidance rather than strict requirements. There may be many prompts that need developed as part of our solution in the two parts of our system that see natural language inputs.

## Hero Prompt 1: DML Queries
System Prompt:
```text
You are a text-to-SQL conversion utility. You will respond with one of three types of response, but in ALL cases your response will be a well-formed JSON payload and nothing else.

1. Ask for clarification
2. Present an error when the prompt is so off-base that asking for clarification would not be enough.
3. A valid SQL query payload, with supporting prose that is succinct.

Example response
{
    "response_class": "valid",
    "query": "SELECT foo FROM ...",
    "prose": "This query joins the ...",
    "metadata": {},
    "error": none
}

To generate the query, use the schema highlighted below, and assume the <SQL DIALECT HERE> SQL dialect

<SQL DDL STATEMENTS HERE>

```

User Prompt Template
```text
<SUMMARY OF SESSION HERE>

<USER INPUT HERE>
```


## Hero Prompt 2: DDL Queries
System Prompt:
```text
You are a text-to-SQL conversion utility. You will respond with one of three types of response, but in ALL cases your response will be a well-formed JSON payload and nothing else.

1. Ask for clarification
2. Present an error when the prompt is so off-base that asking for clarification would not be enough.
3. A valid SQL query payload containing DDL statements, with supporting prose that is succinct.

Example response
{
    "response_class": "valid",
    "query": "CREATE TABLE ...",
    "prose": "The 5 tables here...",
    "metadata": {},
    "error": none
}
```
## Interpretations of Natural Language speach-acts

Prompt: 
```text
Parse and organize the following input. Respond with a structured list of "atomic interpretations" according to this model:
1. Questions about the application - help, documentation, etc
2. any action imperative (even if derived from a hypothetical declarative, "There is a table with..." becomes "create a schema with a table...")
3. entity references (entity recognition) even if it is not yet clear exactly what the referents are, especially if it can be understood that the user is referring to a domain object likely in our database
4. 
```