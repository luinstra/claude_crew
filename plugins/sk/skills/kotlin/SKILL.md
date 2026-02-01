---
name: kotlin
description: Kotlin patterns and best practices. Use when writing Kotlin code, .kt files, .kts files, data classes, coroutines, null safety, extension functions, or Kotlin services.
---

# Kotlin Patterns

## Data Classes

```kotlin
data class Order(
    val id: OrderId,
    val customerId: CustomerId,
    val items: List<OrderItem>,
    val status: OrderStatus,
    val createdAt: Instant,
)

// Use copy() for immutable updates
val updated = order.copy(status = OrderStatus.SHIPPED)
```

## Identifier Types

Use typed identifiers instead of raw types:

```kotlin
// DO: - Type-safe identifiers
@JvmInline
value class OrderId(val value: UUID)

@JvmInline
value class CustomerId(val value: Long)

fun findOrder(id: OrderId): Order?

// DON'T: - Raw types
fun findOrder(id: UUID): Order?
fun findOrder(id: Long): Order?  // Ambiguous!
```

## Null Safety (Strict)

**`!!` (not-null assertion) is FORBIDDEN in main source code.** Use `requireNotNull`, `checkNotNull`, or refactor.

```kotlin
// DO: - Use requireNotNull (stdlib)
val id = requireNotNull(order.id) { "Order ID cannot be null" }

// DO: - Use checkNotNull for state checks
val user = checkNotNull(currentUser) { "User must be logged in" }

// DO: - Safe calls and elvis operator
val name = user?.profile?.displayName ?: "Unknown"

// DO: - let for nullable transformations
user?.let { processUser(it) }

// DON'T: FORBIDDEN in main src
val id = order.id!!  // NEVER do this

// EXCEPTION: Allowed in TESTS ONLY with comment
val result = response.body!! // Test: response is mocked to be non-null
```

## Preconditions

```kotlin
// requireNotNull - throws IllegalArgumentException
val user = requireNotNull(findUser(id)) { "User not found: $id" }

// require - validates arguments
require(order.items.isNotEmpty()) { "Order must have items" }

// checkNotNull - throws IllegalStateException
val session = checkNotNull(currentSession) { "No active session" }

// check - validates state
check(isInitialized) { "Service not initialized" }
```

## Extension Functions

```kotlin
// Good: Domain-specific extensions
fun Order.isShippable(): Boolean =
    status == OrderStatus.PAID && items.all { it.inStock }

// Good: Collection utilities
fun <T> List<T>.secondOrNull(): T? = getOrNull(1)

// Avoid: Extensions that should be methods
// DON'T: fun Order.save(repo: OrderRepository) - should be repo.save(order)
```

## Collection Operations

```kotlin
// Prefer single-pass operations
val orderIds = orders.mapTo(mutableSetOf()) { it.id }

// Or use stdlib
val orderIds = orders.map { it.id }.toSet()

// Avoid multiple iterations
// DON'T: orders.filter { }.map { }.filter { } when one pass suffices
```

## Logging

Use a logging facade like kotlin-logging:

```kotlin
import io.github.oshai.kotlinlogging.KotlinLogging

private val logger = KotlinLogging.logger {}

// Usage
logger.info { "Processing order $orderId" }
logger.debug { "Details: $details" }
logger.error(exception) { "Failed to process order" }
```

## Testing

For comprehensive testing guidance, see the **sk:kotlin-testing** skill.

**Quick reference:**
- Use `TestIdProvider` for all IDs
- Use `TestDateTimeProvider` for all timestamps
- Never use `UUID.randomUUID()` or `now()` in tests

## Code Quality

### Imports

**Always import classes** - never use fully qualified names inline.

```kotlin
// DO: - import at top, use short name
import io.github.oshai.kotlinlogging.KotlinLogging

private val logger = KotlinLogging.logger {}

// DON'T: - inline fully qualified names
private val logger = io.github.oshai.kotlinlogging.KotlinLogging.logger {}
```

Exception: Name collisions - use import aliases (`import com.foo.User as ApiUser`).

### Formatting and Lint

```bash
./gradlew spotlessApply detekt
```

**Always run after completing a batch of edits**, not after every file.

### Detekt (Linting)

Key rules:
- Max 2 returns (guard clauses excluded)
- No `print`/`println` calls
- No wildcard imports
- No unused imports

**Avoid using @Suppress** to silence problems whenever practical.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "I'll just use `!!` this once" | Use `requireNotNull` or `checkNotNull`. No exceptions. |
| "I'll add null checks later" | Add them now. Retrofitting null safety is painful. |
| "This extension is cleaner" | If it hides side effects, make it a method. |

## Checklist

When writing Kotlin code:

- [ ] Using typed identifiers (not raw Long/UUID)
- [ ] No `!!` in main src (use `checkNotNull`/`requireNotNull`)
- [ ] Prefer immutable data classes with `copy()`
- [ ] Tests use Kotest + MockK
- [ ] Run `./gradlew spotlessApply detekt` before committing
