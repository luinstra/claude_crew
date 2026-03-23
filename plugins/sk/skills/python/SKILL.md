---
name: python
description: Use this skill for any Python programming task. Activate whenever the user is writing, reviewing, or refactoring Python code — including exception handling (bare except, try/except patterns), logging (replacing print() with the logging module), data modeling (dataclasses, Pydantic BaseModel, choosing between dicts and classes), file path operations (pathlib, os.path conversion), type hints, enums, pattern matching, string formatting, and comprehensions. Also activate for Python project setup and tooling: uv dependency management, pyproject.toml configuration, ruff linting/formatting, and pytest. Trigger on mentions of Python libraries like pydantic, httpx, or any .py file work. If the user mentions Python idioms, refactoring Python code, or any Python ecosystem tool, use this skill.
---

# Python Patterns

Modern Python 3.14+ conventions. Type-safe, explicit, no magic.

## Data Modeling

### The Rule

**Use classes for structured data. Dicts are for truly dynamic/unknown-shape data only.**

| Use Case | Tool | Why |
|----------|------|-----|
| Domain models, internal data | `dataclass` | Lightweight, stdlib, no deps |
| API responses, config, I/O boundaries | Pydantic `BaseModel` | Validation, serialization, schema |
| Truly dynamic keys/shapes | `dict` | Only when structure is unknowable |

### Dataclasses (Domain Models)

```python
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

@dataclass(frozen=True, slots=True)
class Order:
    id: UUID
    customer_id: UUID
    items: list[OrderItem]
    status: OrderStatus
    created_at: datetime

# frozen=True makes it immutable (like Kotlin data class)
# slots=True improves memory and attribute access speed
```

Use `frozen=True` by default. Mutable dataclasses are the exception, not the rule.

```python
# Immutable update (like Kotlin's copy())
from dataclasses import replace

updated = replace(order, status=OrderStatus.SHIPPED)
```

### Pydantic (I/O Boundaries)

```python
from pydantic import BaseModel, Field

class CreateOrderRequest(BaseModel):
    customer_id: UUID
    items: list[OrderItemRequest]
    notes: str = Field(default="", max_length=500)

    model_config = {"strict": True}

class OrderResponse(BaseModel):
    id: UUID
    status: str
    total: Decimal
```

**Pydantic at the edges, dataclasses in the core.** Don't let Pydantic models leak into your domain logic.

### Don't Do This

```python
# DON'T: untyped dict soup
def create_order(data: dict) -> dict:
    return {"id": uuid4(), "status": "pending", **data}

# DON'T: TypedDict as a substitute for a real model
class Order(TypedDict):
    id: str
    status: str  # No validation, no methods, no immutability

# DO: proper model
@dataclass(frozen=True, slots=True)
class Order:
    id: UUID
    status: OrderStatus
```

## Type Hints

**All function signatures must have type hints.** No exceptions for "simple" functions.

```python
# DO:
def find_user(user_id: UUID) -> User | None:
    ...

def process_orders(orders: list[Order]) -> list[ProcessedOrder]:
    ...

# DON'T:
def find_user(user_id):
    ...

def process_orders(orders):
    ...
```

### Modern Syntax (3.10+)

```python
# Union types — use pipe syntax
name: str | None = None           # not Optional[str]
result: Order | Error             # not Union[Order, Error]

# Built-in generics — no imports needed
items: list[str]                  # not List[str]
mapping: dict[str, int]           # not Dict[str, int]
unique: set[UUID]                 # not Set[UUID]
pair: tuple[str, int]             # not Tuple[str, int]
```

### Type Aliases

```python
# Use the type statement (3.12+)
type OrderId = UUID
type UserId = UUID
type Headers = dict[str, str]
```

## Enums

Use `StrEnum` for string-valued enums (serialization-friendly):

```python
from enum import StrEnum, auto

class OrderStatus(StrEnum):
    PENDING = auto()    # "pending"
    PAID = auto()       # "paid"
    SHIPPED = auto()    # "shipped"
    CANCELLED = auto()  # "cancelled"

# Works naturally with match
match order.status:
    case OrderStatus.PENDING:
        process_payment(order)
    case OrderStatus.PAID:
        ship_order(order)
    case OrderStatus.SHIPPED:
        notify_customer(order)
    case OrderStatus.CANCELLED:
        refund_order(order)
```

## Pattern Matching

Use `match` for complex branching — cleaner than if/elif chains:

```python
match event:
    case OrderCreated(order_id=oid, customer=c):
        notify_customer(c, oid)
    case PaymentReceived(amount=a) if a > 0:
        process_payment(a)
    case PaymentReceived(amount=a):
        log_zero_payment(a)
    case _:
        logger.warning(f"Unhandled event: {event}")
```

## Error Handling

### Specific Exceptions

```python
# DO: catch specific exceptions with context
try:
    user = repository.find_by_id(user_id)
except DatabaseConnectionError:
    logger.error("Database unavailable", exc_info=True)
    raise ServiceUnavailableError("Try again later") from None
except RecordNotFoundError:
    raise UserNotFoundError(user_id)

# DON'T: bare except or overly broad
try:
    user = repository.find_by_id(user_id)
except Exception:
    return None  # Silently swallows everything
```

### Custom Exceptions

```python
class AppError(Exception):
    """Base for all application errors."""

class UserNotFoundError(AppError):
    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        super().__init__(f"User not found: {user_id}")

class ValidationError(AppError):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(f"{field}: {message}")
```

## Logging

Use `logging`, never `print`:

```python
import logging

logger = logging.getLogger(__name__)

# Lazy formatting — don't build the string if the level is disabled
logger.info("Processing order %s", order_id)
logger.debug("Order details: %s", order)
logger.error("Failed to process order %s", order_id, exc_info=True)

# DON'T:
print(f"Processing order {order_id}")  # No level, no control, no structure
logger.info(f"Processing order {order_id}")  # Builds string even if INFO is off
```

## File & Path Handling

Use `pathlib`, not `os.path`:

```python
from pathlib import Path

# DO:
config_path = Path("config") / "settings.toml"
if config_path.exists():
    content = config_path.read_text()

# Iterate files
for py_file in Path("src").rglob("*.py"):
    process(py_file)

# DON'T:
import os
config_path = os.path.join("config", "settings.toml")
if os.path.exists(config_path):
    with open(config_path) as f:
        content = f.read()
```

## String Formatting

**f-strings for everything.** No `.format()`, no `%` formatting:

```python
# DO:
message = f"Order {order.id} shipped to {order.customer.name}"
query = f"SELECT * FROM users WHERE id = {user_id!r}"

# DON'T:
message = "Order {} shipped to {}".format(order.id, order.customer.name)
message = "Order %s shipped to %s" % (order.id, order.customer.name)
```

**Exception:** logger calls use `%s` formatting (see Logging section above) because it defers string construction.

## Project Setup

### pyproject.toml

All project config lives in `pyproject.toml`. No `setup.py`, no `requirements.txt`, no `setup.cfg`.

```toml
[project]
name = "my-service"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = [
    "pydantic>=2.0",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "ruff>=0.8",
    "mypy>=1.13",
]

[tool.ruff]
target-version = "py314"
line-length = 120

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "F",    # pyflakes
    "I",    # isort
    "N",    # pep8-naming
    "UP",   # pyupgrade
    "B",    # flake8-bugbear
    "SIM",  # flake8-simplify
    "RUF",  # ruff-specific rules
]

[tool.mypy]
strict = true
python_version = "3.14"
warn_return_any = true
warn_unused_configs = true

[tool.pytest.ini_options]
testpaths = ["tests"]
```

### uv (Package Manager)

**Always use `uv`, never `pip` or `pip install`.**

```bash
# Create a new project
uv init my-project
cd my-project

# Add dependencies
uv add pydantic httpx

# Add dev dependencies
uv add --group dev pytest ruff mypy

# Run commands in the project environment
uv run python main.py
uv run pytest
uv run ruff check .

# Sync environment from lockfile
uv sync

# Update lockfile
uv lock
```

**Key files:**
- `pyproject.toml` — project config and dependencies
- `uv.lock` — lockfile (commit this)
- `.python-version` — pinned Python version

```bash
# Pin Python version
uv python pin 3.14

# Install a specific Python version
uv python install 3.14
```

### ruff (Formatting & Linting)

```bash
# Check for issues
uv run ruff check .

# Fix auto-fixable issues
uv run ruff check --fix .

# Format code
uv run ruff format .

# Run both (typical workflow)
uv run ruff format . && uv run ruff check --fix .
```

**Run after completing a batch of edits**, not after every file.

## Common Patterns

### Context Managers

```python
# For resource cleanup
from contextlib import contextmanager

@contextmanager
def database_transaction(db: Database):
    conn = db.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# Usage
with database_transaction(db) as conn:
    conn.execute(query)
```

### Comprehensions Over Loops

```python
# DO: comprehension for transforms
order_ids = [order.id for order in orders]
active = {u.id: u for u in users if u.is_active}

# DON'T: loop for simple transforms
order_ids = []
for order in orders:
    order_ids.append(order.id)

# EXCEPTION: use a loop when the body has side effects or is complex
for order in orders:
    send_notification(order.customer)
    update_inventory(order.items)
```

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "It's just a script, dicts are fine" | Scripts become services. Use dataclasses from the start. |
| "Type hints slow me down" | They save 10x the time in debugging later. |
| "I'll just `pip install` real quick" | Use `uv add`. Keep the lockfile clean. |
| "print() is fine for debugging" | Use `logger.debug()`. Print statements get committed. |
| "I know the shape, I don't need a class" | You know it. The next person doesn't. |
| "bare except catches everything" | That's the problem. Catch what you expect. |

## Checklist

When writing Python code:

- [ ] All function signatures have type hints
- [ ] Structured data uses dataclasses or Pydantic (not raw dicts)
- [ ] Pydantic at I/O boundaries, dataclasses for domain models
- [ ] Using `uv` for dependency management (not pip)
- [ ] Project config in `pyproject.toml` (no setup.py/requirements.txt)
- [ ] Using `pathlib` for file paths (not os.path)
- [ ] Specific exception handling (no bare `except:`)
- [ ] Logging via `logging` module (no print statements)
- [ ] Run `uv run ruff format . && uv run ruff check --fix .` before committing
