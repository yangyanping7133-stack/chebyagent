import java.net.URI
import java.security.MessageDigest
import org.gradle.api.tasks.Sync

plugins {
    id("com.android.application")
}

val upstreamRoot = rootProject.projectDir.parentFile.resolve("third_party/termux-app")
val bootstrapCppDirectory = upstreamRoot.resolve("app/src/main/cpp")
val bootstrapArchive = bootstrapCppDirectory.resolve("bootstrap-aarch64.zip")
val bootstrapSha256 = "c8d702b6f742935001c37cda81b8ac69504a95d5cf28f2899532dd8cd4b057eb"
val bootstrapUrl =
    "https://github.com/termux/termux-packages/releases/download/" +
        "bootstrap-2025.03.28-r1+apt-android-7/bootstrap-aarch64.zip"
val generatedRuntimeAssets = layout.buildDirectory.dir("generated/chebyRuntimeAssets")
val runtimeSourceDirectory = projectDir.resolve("runtime")
val repositoryRoot = rootProject.projectDir.parentFile
val projectLicense = repositoryRoot.resolve("LICENSE")
val projectNotice = repositoryRoot.resolve("NOTICE")
val thirdPartyNotices = repositoryRoot.resolve("THIRD_PARTY_NOTICES.md")
val runtimeBundleDirectory =
    repositoryRoot.resolve("artifacts/private/runtime/standalone-4.1")
val codexArchive = repositoryRoot.resolve("artifacts/private/runtime/codex-0.153.4/codex-linux-arm64.tgz")
val codexArchiveSha256 = "439c0dd0d6923f607b4e5cd1e3079c12f0b86f6e5007f07e377d6ad25e2d7bb9"
val bundledSkills = mapOf(
    "phone-ui-recovery" to repositoryRoot.resolve("skills/phone-ui-recovery"),
    "yandex-maps-restaurant-finder" to repositoryRoot.resolve("skills/yandex-maps-restaurant-finder"),
    "yandex-maps-supermarket-finder" to repositoryRoot.resolve("skills/yandex-maps-supermarket-finder"),
    "yandex-maps-haircut-finder" to repositoryRoot.resolve("skills/yandex-maps-haircut-finder"),
    "yandex-maps-massage-finder" to repositoryRoot.resolve("skills/yandex-maps-massage-finder"),
    "yandex-maps-dog-grooming" to repositoryRoot.resolve("skills/yandex-maps-dog-grooming"),
    "cian-rental-finder" to repositoryRoot.resolve("skills/cian-rental-finder"),
    "yandex-go-food-order" to repositoryRoot.resolve("skills/yandex-go-food-order"),
    "russia-menu-assistant" to repositoryRoot.resolve("skills/russia-menu-assistant"),
    "yandex-go-taxi-booker" to repositoryRoot.resolve("skills/yandex-go-taxi-booker"),
    "yandex-maps-coffee-finder" to repositoryRoot.resolve("skills/yandex-maps-coffee-finder"),
    "yandex-maps-route-planner" to repositoryRoot.resolve("skills/yandex-maps-route-planner"),
)
val runtimeBundles = mapOf(
    "debian-rootfs-aarch64.tar.zst" to
        "ef6acc3d5842700dfc879e3ca83532a4b2c495ebb8a4a7cac645cbfd7aa56c7e",
    "termux-proot-overlay-aarch64.tar.zst" to
        "334a0e6aa93cf3264f416879d4a95ee5ad9df52a9c202f12436b07eaf7bc067c",
)

fun sha256(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    file.inputStream().buffered().use { input ->
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            digest.update(buffer, 0, count)
        }
    }
    return digest.digest().joinToString("") { byte -> "%02x".format(byte.toInt() and 0xff) }
}

val prepareTermuxBootstrap = tasks.register("prepareTermuxBootstrap") {
    inputs.property("url", bootstrapUrl)
    inputs.property("sha256", bootstrapSha256)
    outputs.file(bootstrapArchive)
    doLast {
        if (bootstrapArchive.isFile) {
            require(sha256(bootstrapArchive) == bootstrapSha256) {
                "Pinned Termux bootstrap exists but its SHA-256 is wrong: $bootstrapArchive"
            }
            return@doLast
        }
        bootstrapCppDirectory.mkdirs()
        val temporary = bootstrapCppDirectory.resolve("bootstrap-aarch64.zip.part")
        URI(bootstrapUrl).toURL().openStream().buffered().use { input ->
            temporary.outputStream().buffered().use(input::copyTo)
        }
        require(sha256(temporary) == bootstrapSha256) {
            "Downloaded Termux bootstrap SHA-256 does not match the pinned lock"
        }
        require(temporary.renameTo(bootstrapArchive)) {
            "Could not activate the verified Termux bootstrap"
        }
    }
}

val prepareChebyRuntimeAssets = tasks.register<Sync>("prepareChebyRuntimeAssets") {
    inputs.file(codexArchive)
    inputs.files(projectLicense, projectNotice, thirdPartyNotices)
    inputs.property("codex-linux-arm64.tgz.sha256", codexArchiveSha256)
    runtimeBundles.forEach { (name, digest) ->
        val bundle = runtimeBundleDirectory.resolve(name)
        inputs.file(bundle)
        inputs.property("$name.sha256", digest)
    }
    bundledSkills.forEach { (name, directory) ->
        inputs.file(directory.resolve("SKILL.md"))
        inputs.file(directory.resolve("agents/openai.yaml"))
        from(directory.resolve("SKILL.md")) {
            rename { "skill-$name.md" }
        }
        from(directory.resolve("agents/openai.yaml")) {
            rename { "skill-$name.openai.yaml" }
        }
    }
    doFirst {
        require(codexArchive.isFile && sha256(codexArchive) == codexArchiveSha256) {
            "Pinned offline Codex archive is missing or has the wrong SHA-256: $codexArchive"
        }
        runtimeBundles.forEach { (name, digest) ->
            val bundle = runtimeBundleDirectory.resolve(name)
            require(bundle.isFile) {
                "Pinned offline runtime bundle is missing: $bundle"
            }
            require(sha256(bundle) == digest) {
                "Pinned offline runtime bundle has the wrong SHA-256: $bundle"
            }
        }
    }
    into(generatedRuntimeAssets.map { it.dir("cheby-runtime") })
    from(runtimeSourceDirectory) {
        include("*.sh", "*.py", "runtime.lock", "codex-base-instructions.md")
    }
    from(codexArchive)
    from(runtimeBundleDirectory) {
        include(runtimeBundles.keys)
    }
    from(repositoryRoot.resolve("tools/standalone/phone_start_codex_appserver.sh")) {
        rename { "start-codex-appserver.sh" }
    }
    from(repositoryRoot.resolve("tools/standalone/phone_start_phonebridge.sh")) {
        rename { "start-phonebridge.sh" }
    }
    from(repositoryRoot.resolve("connector/cheby_connector/local_mcp.py")) {
        rename { "local_mcp.py" }
    }
    from(repositoryRoot.resolve("connector/cheby_connector/ace_memory.py"))
    from(repositoryRoot.resolve("connector/cheby_connector/ace_core")) {
        include("*.py", "LICENSE", "UPSTREAM.md")
        rename { "ace-core-$it" }
    }
    from(repositoryRoot.resolve("skills/yandex-maps-coffee-finder/references/poster.md")) {
        rename { "coffee-poster-reference.md" }
    }
    from(repositoryRoot.resolve("skills/cian-rental-finder/references/report.md")) {
        rename { "cian-rental-report-reference.md" }
    }
    from(runtimeSourceDirectory.resolve("mobile-experience-instructions.md"))
    from(projectLicense) {
        rename { "LICENSE-GPL-3.0.txt" }
    }
    from(projectNotice)
    from(thirdPartyNotices)
    from(repositoryRoot.resolve("connector/phonebridge/phonebridge.mjs")) {
        rename { "phonebridge.mjs" }
    }
    doLast {
        val assetDirectory = generatedRuntimeAssets.get().dir("cheby-runtime").asFile
        val manifest = assetDirectory.resolve("runtime-assets.sha256")
        val entries = assetDirectory.listFiles()
            .orEmpty()
            .filter { it.isFile && it.name != manifest.name }
            .sortedBy(File::getName)
            .joinToString(separator = "\n", postfix = "\n") { file ->
                "${sha256(file)}  ${file.name}"
            }
        manifest.writeText(entries)
    }
}

configurations.configureEach {
    // Guava 24 already contains ListenableFuture; modern AndroidX also requests its split artifact.
    exclude(group = "com.google.guava", module = "listenablefuture")
}

android {
    namespace = "com.termux"
    compileSdk = 35
    ndkVersion = "27.2.12479018"

    defaultConfig {
        applicationId = "com.termux"
        minSdk = 28
        targetSdk = 28
        versionCode = 19
        versionName = "0.7.3"

        ndk {
            abiFilters += "arm64-v8a"
        }
    }

    sourceSets {
        getByName("main") {
            manifest.srcFile("src/main/AndroidManifest.xml")
            java.srcDir(upstreamRoot.resolve("app/src/main/java"))
            res.srcDir(upstreamRoot.resolve("app/src/main/res"))
            assets.srcDir(generatedRuntimeAssets)
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    externalNativeBuild {
        ndkBuild {
            path = bootstrapCppDirectory.resolve("Android.mk")
        }
    }

    packaging {
        jniLibs.useLegacyPackaging = true
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }

    androidResources {
        noCompress += "zst"
        noCompress += "tgz"
    }

    lint {
        abortOnError = true
        warningsAsErrors = false
        disable += "ProtectedPermissions"
        // GitHub-distributed prototype: target 28 is required by the pinned executable prefix.
        disable += "ExpiredTargetSdkVersion"
    }
}

tasks.named("preBuild").configure {
    dependsOn(prepareTermuxBootstrap, prepareChebyRuntimeAssets)
}

dependencies {
    implementation(project(":embedded:chebyUi"))
    implementation(project(":embedded:phoneNode"))
    implementation(project(":embedded:runtimeApi"))
    implementation(project(":embedded:termuxShared"))
    implementation(project(":embedded:termuxTerminalView"))
    implementation(project(":embedded:termuxTerminalEmulator"))

    implementation("androidx.annotation:annotation:1.3.0")
    implementation("androidx.appcompat:appcompat:1.3.1")
    implementation("androidx.core:core:1.6.0")
    implementation("androidx.drawerlayout:drawerlayout:1.1.1")
    implementation("androidx.preference:preference:1.1.1")
    implementation("androidx.viewpager:viewpager:1.0.0")
    implementation("com.google.android.material:material:1.4.0")
    implementation("com.google.guava:guava:24.1-jre")
    implementation("io.noties.markwon:core:4.6.2")
    implementation("io.noties.markwon:ext-strikethrough:4.6.2")
    implementation("io.noties.markwon:linkify:4.6.2")
    implementation("io.noties.markwon:recycler:4.6.2")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
