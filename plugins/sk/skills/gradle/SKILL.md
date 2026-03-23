---
name: gradle
description: Use this skill for ANY work involving Gradle builds, including: modifying build.gradle.kts, settings.gradle.kts, or libs.versions.toml files; adding, removing, or bumping dependency versions; working with Gradle version catalogs (adding libraries, bundles, or plugins to the catalog); regenerating or fixing lockfiles; configuring Gradle tasks like test, compile, or custom tasks (e.g., setting JVM args, parallelism, heap size); troubleshooting build failures related to dependencies or stale lockfiles; and running or writing ./gradlew commands. Activate whenever the user mentions version catalogs, dependency management in Kotlin/JVM projects, lockfile errors, Gradle task configuration, or any reference to toml-based dependency definitions. Also activate when moving hardcoded dependency versions into a version catalog, creating dependency bundles, or resolving "lockfile out of date" errors.
---

# Gradle Kotlin DSL Patterns

Gradle build configuration conventions for Kotlin projects.

## Version Catalog

Dependencies are managed in `gradle/libs.versions.toml`:

```toml
[versions]
kotlin = "2.0.21"
exposed = "0.56.0"
kotest = "5.9.1"

[libraries]
exposed-core = { module = "org.jetbrains.exposed:exposed-core", version.ref = "exposed" }
exposed-jdbc = { module = "org.jetbrains.exposed:exposed-jdbc", version.ref = "exposed" }
kotest-runner = { module = "io.kotest:kotest-runner-junit5", version.ref = "kotest" }
kotest-assertions = { module = "io.kotest:kotest-assertions-core", version.ref = "kotest" }

[bundles]
exposed = ["exposed-core", "exposed-jdbc"]
kotest = ["kotest-runner", "kotest-assertions"]

[plugins]
kotlin-jvm = { id = "org.jetbrains.kotlin.jvm", version.ref = "kotlin" }
```

### Using in build.gradle.kts

```kotlin
dependencies {
    // Single library
    implementation(libs.exposed.core)

    // Bundle (multiple libraries)
    implementation(libs.bundles.exposed)

    // Test dependencies
    testImplementation(libs.bundles.kotest)
}
```

## Adding a New Dependency

1. **Add to version catalog** (`gradle/libs.versions.toml`):
```toml
[versions]
newlib = "1.2.3"

[libraries]
newlib-core = { module = "com.example:newlib-core", version.ref = "newlib" }
```

2. **Reference in build.gradle.kts**:
```kotlin
dependencies {
    implementation(libs.newlib.core)
}
```

3. **Update lockfiles** (if using dependency locking):
```bash
./gradlew resolveAndLockAll --write-locks
```

4. **Commit both files**:
- `gradle/libs.versions.toml`
- `gradle.lockfile` (if using locking)

## Lockfiles

Lockfiles ensure reproducible builds:

```bash
# Update all lockfiles
./gradlew resolveAndLockAll --write-locks

# Verify lockfiles are current (CI)
./gradlew build  # Fails if lockfiles are stale
```

## Common Tasks

```bash
# Build
./gradlew build                     # Build all modules
./gradlew :module-name:build        # Build specific module
./gradlew compileKotlin             # Compile only

# Test
./gradlew test                      # Run all tests
./gradlew test -i                   # Verbose output
./gradlew :module:test              # Test specific module
./gradlew :module:test --tests "ClassName"           # Single class
./gradlew :module:test --tests "*ClassName.testName" # Single test

# Code Quality
./gradlew spotlessCheck             # Check formatting
./gradlew spotlessApply             # Fix formatting
./gradlew detekt                    # Run linter
./gradlew check                     # All quality checks

# Dependencies
./gradlew dependencies              # Show dependency tree
./gradlew dependencyInsight --dependency exposed-core  # Specific dependency

# Clean
./gradlew clean                     # Remove build outputs
./gradlew clean build               # Clean build
```

## Multi-Module Structure

```
project/
├── build.gradle.kts          # Root build file
├── settings.gradle.kts       # Module discovery
├── gradle/
│   └── libs.versions.toml    # Version catalog
├── gradle.lockfile           # Root lockfile
├── module-a/
│   ├── build.gradle.kts
│   └── gradle.lockfile
└── module-b/
    ├── build.gradle.kts
    └── gradle.lockfile
```

### settings.gradle.kts

```kotlin
rootProject.name = "my-project"

// Include subprojects
include(":module-a")
include(":module-b")

// Or use auto-discovery
rootDir.listFiles()
    ?.filter { it.isDirectory && File(it, "build.gradle.kts").exists() }
    ?.forEach { include(":${it.name}") }
```

### Module build.gradle.kts

```kotlin
plugins {
    alias(libs.plugins.kotlin.jvm)
}

dependencies {
    implementation(project(":module-a"))  // local project dependency
    implementation(libs.exposed.core)      // External dependency

    testImplementation(libs.bundles.kotest)
}
```

## Writing Custom Tasks

```kotlin
tasks.register("myTask") {
    group = "custom"
    description = "Does something useful"

    doLast {
        println("Running my task")
    }
}

// Task with inputs/outputs (for caching)
tasks.register<Copy>("copyConfigs") {
    from("src/main/resources/config")
    into("$buildDir/configs")
}

// Task depending on another
tasks.register("fullBuild") {
    dependsOn("build", "test", "spotlessApply")
}
```

## Gradle Properties

`gradle.properties` for project-wide settings:

```properties
# JVM settings
org.gradle.jvmargs=-Xmx2g -XX:+UseParallelGC

# Parallel builds
org.gradle.parallel=true
org.gradle.caching=true

# Kotlin
kotlin.code.style=official

# Custom properties
myapp.version=1.0.0
```

Access in build.gradle.kts:
```kotlin
val appVersion: String by project
// or
val appVersion = project.findProperty("myapp.version") as String? ?: "0.0.1"
```

## Common Mistakes

**Forgetting to update lockfiles**
```bash
# Added dependency but didn't update lockfile
./gradlew build  # Fails: lockfile out of date

# Fix:
./gradlew resolveAndLockAll --write-locks
```

---

**Hardcoding versions in build.gradle.kts**
```kotlin
// DON'T
implementation("org.jetbrains.exposed:exposed-core:0.56.0")

// DO
implementation(libs.exposed.core)
```

---

**Not committing lockfiles**
```bash
# DON'T
git add gradle/libs.versions.toml
git commit -m "Add new dependency"
# Forgot gradle.lockfile!

# DO
git add gradle/libs.versions.toml gradle.lockfile
git commit -m "feat(deps): add exposed orm"
```

---

**Running spotlessApply after every file**
```bash
# DON'T
# Edit file -> ./gradlew spotlessApply -> repeat

# DO - Run once after completing work
# Edit all files -> ./gradlew spotlessApply -> commit
```

## Checklist

When modifying build configuration:

- [ ] Dependencies added to `gradle/libs.versions.toml`
- [ ] Lockfiles updated (if using locking)
- [ ] Both catalog and lockfiles committed
- [ ] `./gradlew build` passes locally
