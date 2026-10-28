"""LLM gateway (design doc 7.1, 9): one interface over Meta model providers, with a response
cache, replay and mock modes, cost accounting and a spend cap. No non-Meta model is ever called."""
