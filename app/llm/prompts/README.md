# app/llm/prompts/

Every prompt lives here as a file, never inline in code (design doc 11.4 rule 7). Files are named
after their role (`intake.md`, `intake_examples.jsonl`, `explain.md`, ...); the cache key includes a
hash of a role's files, so editing a prompt invalidates only that role's cached responses.
