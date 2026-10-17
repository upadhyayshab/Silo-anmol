"""Unit tests run on mocks and in-memory SQLite, never a real database,
broker or third-party API. config.py calls load_dotenv() on the developer's
local .env, which can point at the shared dev database; making it a no-op
before config is imported keeps every test off those services. Variables the
test command sets explicitly still apply."""
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
