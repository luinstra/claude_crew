---
name: kotlin-testing
description: Kotlin testing patterns. Use when writing tests, test classes, Kotest specs, MockK mocks, TestIdProvider, TestDateTimeProvider, DatabaseTest, Testcontainers, or integration tests.
---

# Kotlin Testing Patterns

## When This Activates

- "write a test" / "add tests" / "test this"
- "kotest" / "funspec" / "test spec" / "junit"
- "mockk" / "mock" / "stub" / "verify"
- "TestIdProvider" / "TestDateTimeProvider"
- "DatabaseTest" / "integration test"
- "testcontainers" / "postgres container"
- "slot" / "capture" / "verify"

---

## Quick Reference

| Framework | Purpose |
|-----------|---------|
| Kotest FunSpec | Test structure and lifecycle |
| MockK | Mocking and verification |
| TestIdProvider | **Mandatory** for ID generation |
| TestDateTimeProvider | **Mandatory** for timestamps |
| Testcontainers | Postgres container for integration tests |

**Golden Rule:** AVOID using `UUID.randomUUID()`, `LocalDate.now()`, or `OffsetDateTime.now()` in tests. Always prefer the test providers.

---

## Test Utilities

### TestIdProvider

**Example Class**

```kotlin
/** Generates consistent, predictable IDs for easier testing */
class TestIdProvider {
    private val counter = AtomicLong(1)

    fun testId(lsb: Long): UUID = UUID(0, lsb)

    fun nextId(): UUID = testId(counter.getAndIncrement())

    fun randomUuid(): UUID = nextId()  // Not actually random - sequential for debugging
}
```

**Always use `TestIdProvider` for generating IDs in tests.**

```kotlin
// DO:
val idProvider = TestIdProvider()
val orderId = OrderId(idProvider.nextId())
val customerId = CustomerId(idProvider.nextId())

// DON'T:
val orderId = OrderId(UUID.randomUUID())  // Random - causes flaky tests
```

**Why?**
- Deterministic, sequential IDs for predictable test behavior
- Makes test failures easier to debug with consistent IDs across runs
- Prevents flaky tests caused by random UUID generation

### TestDateTimeProvider

**Always use `TestDateTimeProvider` for date/time values in tests.**

```kotlin
// DO:
val dateTimeProvider = TestDateTimeProvider()
val now = dateTimeProvider.now()      // OffsetDateTime
val today = dateTimeProvider.today()  // LocalDate

// DON'T:
val now = OffsetDateTime.now()  // Changes every run
val today = LocalDate.now()     // Time-dependent
```

**Example class:**
```kotlin
class TestDateTimeProvider(
    private val fixedDate: LocalDate = LocalDate.of(2025, 10, 1),
    private val fixedDateTime: OffsetDateTime = OffsetDateTime.parse("2025-10-01T00:00:00Z")
) {
    fun now(): OffsetDateTime = fixedDateTime
    fun today(): LocalDate = fixedDate
}
```

**Why?**
- Makes tests deterministic (no flaky time-dependent tests)
- Allows testing temporal behavior without waiting
- Enables date comparisons and edge case testing

---

## Kotest Patterns

### FunSpec Structure

```kotlin
class MyHandlerTest : FunSpec({
    val idProvider = TestIdProvider()
    val dateTimeProvider = TestDateTimeProvider()
    val service = mockk<MyService>()
    lateinit var handler: MyHandler

    beforeEach {
        handler = MyHandler(service)
        clearMocks(service)
    }

    context("feature name") {
        test("should do something when condition") {
            // test implementation
        }
    }
})
```

### Context Blocks

Group related tests using `context`:

```kotlin
class OrderServiceTest : FunSpec({
    val repository = mockk<OrderRepository>()
    val service = OrderService(repository)

    context("creating orders") {
        test("should create order when valid input provided") { /* ... */ }
        test("should throw error when duplicate ID exists") { /* ... */ }
    }

    context("cancelling orders") {
        test("should cancel order when it exists") { /* ... */ }
        test("should throw error when order not found") { /* ... */ }
    }
})
```

### Test Naming

**Pattern:** `"should [expected behavior] when [condition]"`

```kotlin
// DO:
test("should create order when valid input provided") { /* ... */ }
test("should throw error when customer ID is invalid") { /* ... */ }

// DON'T:
test("test create") { /* ... */ }
test("testError") { /* ... */ }
```

### Lifecycle Hooks

| Hook | When | Use Case |
|------|------|----------|
| `beforeSpec` | Once before all tests in spec | Container startup |
| `afterSpec` | Once after all tests in spec | Container cleanup |
| `beforeEach` / `beforeTest` | Before each test | Reset mocks |
| `afterEach` / `afterTest` | After each test | Clean test data |

---

## MockK Patterns

### Creating Mocks

```kotlin
// Strict mock (throws on unstubbed calls)
val repository = mockk<OrderRepository>()

// Relaxed mock (returns default values for unstubbed calls)
val logger = mockk<Logger>(relaxed = true)
```

### Stubbing

```kotlin
// Return a value
every { repository.findById(any()) } returns order

// Return null
every { repository.findById(any()) } returns null

// Throw an exception
every { repository.save(any()) } throws IllegalStateException("DB error")

// Return different values on successive calls
every { repository.findById(any()) } returnsMany listOf(null, order)
```

### Verification

```kotlin
// Verify method was called
verify { repository.save(order) }

// Verify method was NOT called
verify(exactly = 0) { repository.delete(any()) }

// Verify call count
verify(exactly = 2) { repository.findById(any()) }

// Verify order of calls
verifyOrder {
    repository.findById(any())
    repository.save(any())
}
```

### Slot-Based Capture (Important!)

Use slots to capture and validate arguments passed to mocks:

```kotlin
val entitySlot = slot<Entity>()
every { service.create(capture(entitySlot)) } returns mockk()

handler.handleRequest(request)

verify { service.create(any()) }

// Inspect captured argument
val captured = entitySlot.captured
captured.id shouldBe expectedId
captured.name shouldBe "Test Entity"
captured.created shouldBe dateTimeProvider.now()
```

**Why slots?**
- Verify mapped objects have correct field values
- Test that transformations work correctly
- Debug test failures by inspecting captured values

---

## Integration Tests

### With Testcontainers

```kotlin
class UserRepositoryTest : FunSpec({
    val postgres = PostgreSQLContainer("postgres:16")
    lateinit var database: Database
    lateinit var repository: UserRepository

    beforeSpec {
        postgres.start()
        database = Database.connect(
            postgres.jdbcUrl,
            user = postgres.username,
            password = postgres.password
        )
        // Run migrations
        repository = UserRepository(database)
    }

    afterSpec {
        postgres.stop()
    }

    test("should create and find user") {
        val idProvider = TestIdProvider()
        val user = User(
            id = UserId(idProvider.nextId()),
            name = "Test User"
        )

        repository.create(user)
        val found = repository.findById(user.id)

        found shouldBe user
    }
})
```

### Container Timing

**Tests can fail if containers haven't fully spun down from a previous run.**

If you see connection errors:
1. **Wait 5-10 seconds** after a test run before starting another
2. Check for orphaned containers: `docker ps -a | grep postgres`
3. Clean up manually if needed: `docker stop $(docker ps -q --filter ancestor=postgres:16)`

---

## Common Test Patterns

### Arrange-Act-Assert

```kotlin
test("should create order when valid input provided") {
    // Arrange - Set up test data and mocks
    val orderId = OrderId(idProvider.nextId())
    val order = Order(id = orderId, name = "Test Order")
    every { repository.create(order) } returns order

    // Act - Execute the code under test
    val result = service.create(order)

    // Assert - Verify the results
    result shouldBe order
    verify { repository.create(order) }
}
```

---

## Common Mistakes

**Using random IDs or current dates**

```kotlin
// DON'T:
val orderId = UUID.randomUUID()
val now = OffsetDateTime.now()

// DO:
val orderId = idProvider.nextId()
val now = dateTimeProvider.now()
```

**Missing verify calls**

```kotlin
// DON'T:
test("should call repository") {
    service.create(order)
    // Missing verify!
}

// DO:
test("should call repository create method") {
    service.create(order)
    verify { repository.create(order) }
}
```

**Tests depending on execution order**

```kotlin
// DON'T: Relying on state from previous test
test("should find created order") {
    val order = repository.findById(previouslyCreatedId)  // What ID?
}

// DO: Each test creates its own data
test("should find created order") {
    val order = Order(id = OrderId(idProvider.nextId()))
    repository.create(order)
    val found = repository.findById(order.id)
    found shouldBe order
}
```

---

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Just this once I'll use UUID.randomUUID()" | Use TestIdProvider. Debugging flaky tests is worse. |
| "The test is simple, I don't need AAA comments" | Future you will thank present you. Add the comments. |
| "I'll verify the mock call later" | Add verify now. Missing verifications cause silent failures. |
| "This helper makes tests cleaner" | Helpers that hide test data make failures harder to debug. |
| "I'll just call now() in the test" | TestDateTimeProvider exists. Use it. |

---

## Test Checklist

When writing a new test:

**Setup:**
- [ ] Use `TestIdProvider` for all IDs
- [ ] Use `TestDateTimeProvider` for all dates/times
- [ ] Create mocks with `mockk<Type>()`
- [ ] Stub methods with `every { ... } returns ...`

**Structure:**
- [ ] Descriptive test name: `"should [behavior] when [condition]"`
- [ ] Follow Arrange-Act-Assert pattern
- [ ] Use `context` blocks to group related tests
- [ ] Test focused on single behavior

**Verification:**
- [ ] Verify mock interactions with `verify { ... }`
- [ ] Use `verify(exactly = 0)` for methods that shouldn't be called
- [ ] Use slots to capture and validate arguments
- [ ] Assert expected results with matchers (`shouldBe`, `shouldNotBe`, etc.)

**Cleanup:**
- [ ] Tests are independent (no shared state)
- [ ] No random values anywhere
