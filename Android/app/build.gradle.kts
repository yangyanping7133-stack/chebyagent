import java.nio.file.Files
import java.util.Base64
import java.util.zip.ZipFile

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

val relayTrustBundleDir = providers.gradleProperty("relayTrustBundleDir").orNull
val relayTrustBundleFilePath = providers.gradleProperty("relayTrustBundleFile").orNull
val repositoryRoot = rootProject.projectDir.parentFile
val defaultSigningDirectory = repositoryRoot.resolve("artifacts/private/android-signing")
val releaseStoreFilePath = providers.gradleProperty("chebyReleaseStoreFile").orNull
    ?: defaultSigningDirectory.resolve("chebycodex-v01.p12").takeIf(File::isFile)?.absolutePath
val releaseStorePasswordFilePath =
    providers.gradleProperty("chebyReleaseStorePasswordFile").orNull
        ?: defaultSigningDirectory.resolve("release-signing.pass").takeIf(File::isFile)?.absolutePath
val releaseKeyAliasValue = providers.gradleProperty("chebyReleaseKeyAlias").orNull
    ?: "chebycodex-v01"
val releaseKeyPasswordFilePath =
    providers.gradleProperty("chebyReleaseKeyPasswordFile").orNull
        ?: defaultSigningDirectory.resolve("release-key.pass").takeIf(File::isFile)?.absolutePath
fun readSigningSecret(path: String?): String? = path?.let(::file)
    ?.takeIf(File::isFile)
    ?.readText(Charsets.UTF_8)
    ?.trim()
    ?.takeIf { it.isNotEmpty() && it.length <= 1_024 }
val releaseStorePasswordValue = readSigningSecret(releaseStorePasswordFilePath)
val releaseKeyPasswordValue = readSigningSecret(releaseKeyPasswordFilePath)
val stagedRelayTrustAssets = layout.buildDirectory.dir("generated/relay-trust-assets")
val stagedRelayPrivateTrustResources =
    layout.buildDirectory.dir("generated/relay-trust-resources/private-only")
val stagedRelayMigrationTrustResources =
    layout.buildDirectory.dir("generated/relay-trust-resources/migration")
val stageRelayTrustBundle = tasks.register<Copy>("stageRelayTrustBundle") {
    val stagedFile = stagedRelayTrustAssets.get().file("relay-trust-bundle.json").asFile
    doFirst { if (stagedFile.exists()) stagedFile.setWritable(true, true) }
    relayTrustBundleFilePath?.let { source ->
        from(file(source))
        rename { "relay-trust-bundle.json" }
        into(stagedRelayTrustAssets)
    }
    doLast { stagedFile.setWritable(true, true) }
}
val stageRelayTrustResources = tasks.register("stageRelayTrustResources") {
    val privateOutputDirectory = stagedRelayPrivateTrustResources.get().asFile
    val migrationOutputDirectory = stagedRelayMigrationTrustResources.get().asFile
    relayTrustBundleFilePath?.let { source -> inputs.file(file(source)) }
    relayTrustBundleDir?.let { directory ->
        inputs.file(file(directory).resolve("relay-trust-bundle.json"))
    }
    outputs.dirs(privateOutputDirectory, migrationOutputDirectory)
    doLast {
        val bundle = relayTrustBundleFilePath?.let(::file)
            ?: relayTrustBundleDir?.let { file(it).resolve("relay-trust-bundle.json") }
        require(bundle?.isFile == true) {
            "Private-CA builds require a Relay trust bundle"
        }
        val encodedCertificates = Regex(
            """"certificateDerBase64"\s*:\s*"([A-Za-z0-9+/=]+)"""",
        ).findAll(requireNotNull(bundle).readText(Charsets.UTF_8))
            .map { it.groupValues[1] }
            .toList()
        require(encodedCertificates.size in 1..2) {
            "Relay trust bundle must contain one or two private CA certificates"
        }
        val pem = encodedCertificates.joinToString("\n") { encoded ->
            val canonical = Base64.getEncoder().encodeToString(Base64.getDecoder().decode(encoded))
            require(canonical == encoded) { "Relay trust certificate is invalid" }
            canonical.chunked(64).joinToString(
                separator = "\n",
                prefix = "-----BEGIN CERTIFICATE-----\n",
                postfix = "\n-----END CERTIFICATE-----",
            )
        }
        listOf(
            privateOutputDirectory to false,
            migrationOutputDirectory to true,
        ).forEach { (outputDirectory, includeSystemTrust) ->
            val rawDirectory = outputDirectory.resolve("raw").apply { mkdirs() }
            val xmlDirectory = outputDirectory.resolve("xml").apply { mkdirs() }
            rawDirectory.resolve("relay_private_ca.pem").writeText("$pem\n", Charsets.US_ASCII)
            val systemTrust = if (includeSystemTrust) {
                """            <certificates src="system" />"""
            } else {
                ""
            }
            val policy = """
                |<?xml version="1.0" encoding="utf-8"?>
                |<network-security-config>
                |    <base-config cleartextTrafficPermitted="false">
                |        <trust-anchors>
                |            <certificates src="@raw/relay_private_ca" />
                |$systemTrust
                |        </trust-anchors>
                |    </base-config>
                |</network-security-config>
                |
                """.trimMargin()
            xmlDirectory.resolve("network_security_config.xml").writeText(
                policy,
                Charsets.UTF_8,
            )
            require(policy.contains("""<certificates src="@raw/relay_private_ca" />"""))
            require(policy.contains("""<certificates src="system" />""") == includeSystemTrust) {
                "Relay network trust policy does not match its build type"
            }
        }
    }
}

android {
    namespace = "com.cheby.codex.mobile"
    compileSdk = 35
    // Instrumentation is intentionally attached only to the isolated 27462 Gate application.
    testBuildType = "gate"

    defaultConfig {
        applicationId = "com.cheby.codex.mobile"
        minSdk = 28
        targetSdk = 35
        // v6 is the one-time Quick Tunnel migration build; v7 is the fixed-IP cutover.
        // v8 makes the production composer explicitly accessible to PhoneBridge.
        // v9 reconciles ambiguous accepted turns from terminal snapshots and clears IME focus.
        // v10 ACKs a Relay event only after the UI reducer has accepted it.
        // v12 projects correlated structured lifecycle events into the Thread directory.
        versionCode = 12
        versionName = "0.4.5"
        buildConfigField("boolean", "STANDALONE_MODE", "false")

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        vectorDrawables.useSupportLibrary = true
    }

    signingConfigs {
        create("externalRelease") {
            if (
                releaseStoreFilePath != null &&
                releaseStorePasswordValue != null &&
                releaseKeyPasswordValue != null
            ) {
                storeFile = file(releaseStoreFilePath)
                storeType = "PKCS12"
                storePassword = releaseStorePasswordValue
                keyAlias = releaseKeyAliasValue
                keyPassword = releaseKeyPasswordValue
            }
        }
    }

    buildTypes {
        debug {
            buildConfigField("boolean", "DEMO_GATEWAY_ENABLED", "true")
            buildConfigField("boolean", "RELAY_PRIVATE_CA_REQUIRED", "false")
            buildConfigField("boolean", "RELAY_PLATFORM_TRUST_FALLBACK", "true")
            buildConfigField("boolean", "LEGACY_RELAY_MIGRATION_ENABLED", "true")
            buildConfigField(
                "String",
                "LEGACY_RELAY_ORIGIN",
                "\"https://chemicals-submission-capital-indianapolis.trycloudflare.com\"",
            )
            buildConfigField("int", "RELAY_SERVICE_PORT", "27461")
        }
        create("gate") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".gate"
            versionNameSuffix = "-gate"
            matchingFallbacks += listOf("debug")
            buildConfigField("boolean", "DEMO_GATEWAY_ENABLED", "false")
            buildConfigField("boolean", "RELAY_PRIVATE_CA_REQUIRED", "true")
            buildConfigField("boolean", "RELAY_PLATFORM_TRUST_FALLBACK", "false")
            buildConfigField("boolean", "LEGACY_RELAY_MIGRATION_ENABLED", "false")
            buildConfigField("String", "LEGACY_RELAY_ORIGIN", "\"\"")
            buildConfigField("int", "RELAY_SERVICE_PORT", "27462")
            signingConfig = signingConfigs.getByName("debug")
        }
        create("standalone") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".standalone"
            versionNameSuffix = "-standalone"
            matchingFallbacks += listOf("debug")
            buildConfigField("boolean", "STANDALONE_MODE", "true")
            buildConfigField("boolean", "DEMO_GATEWAY_ENABLED", "false")
            buildConfigField("boolean", "LEGACY_RELAY_MIGRATION_ENABLED", "false")
            buildConfigField("String", "LEGACY_RELAY_ORIGIN", "\"\"")
            signingConfig = signingConfigs.getByName("debug")
        }
        release {
            buildConfigField("boolean", "DEMO_GATEWAY_ENABLED", "false")
            buildConfigField("boolean", "RELAY_PRIVATE_CA_REQUIRED", "true")
            buildConfigField("boolean", "RELAY_PLATFORM_TRUST_FALLBACK", "false")
            buildConfigField("boolean", "LEGACY_RELAY_MIGRATION_ENABLED", "false")
            buildConfigField("String", "LEGACY_RELAY_ORIGIN", "\"\"")
            buildConfigField("int", "RELAY_SERVICE_PORT", "27461")
            isMinifyEnabled = true
            isShrinkResources = true
            signingConfig = signingConfigs.getByName("externalRelease")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
        create("migration") {
            initWith(getByName("release"))
            versionNameSuffix = "-migration"
            buildConfigField("boolean", "LEGACY_RELAY_MIGRATION_ENABLED", "true")
            buildConfigField(
                "String",
                "LEGACY_RELAY_ORIGIN",
                "\"https://chemicals-submission-capital-indianapolis.trycloudflare.com\"",
            )
        }
    }

    sourceSets {
        if (relayTrustBundleDir != null) {
            getByName("main").assets.srcDir(file(relayTrustBundleDir))
        } else if (relayTrustBundleFilePath != null) {
            getByName("main").assets.srcDir(stagedRelayTrustAssets)
        }
        // Final, migration and Gate APKs must not inherit Android's public system roots.
        // Generate their global policy from the same APK-signed private CA bundle.
        listOf("release", "gate").forEach { buildType ->
            getByName(buildType).res.srcDir(stagedRelayPrivateTrustResources)
        }
        // The one-use migration APK must keep its existing Cloudflare Quick Tunnel reachable while
        // it authenticates the private-CA fixed edge. RelayTlsTrust still applies the private CA and
        // exact IP:port policy to the candidate origin; only this migration variant trusts system
        // roots at the app-wide network-security layer.
        getByName("migration").res.srcDir(stagedRelayMigrationTrustResources)
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
}

val verifyRelayTrustPolicyMapping = tasks.register("verifyRelayTrustPolicyMapping") {
    dependsOn(stageRelayTrustResources)
    doLast {
        val privateDirectory = stagedRelayPrivateTrustResources.get().asFile.canonicalFile
        val migrationDirectory = stagedRelayMigrationTrustResources.get().asFile.canonicalFile
        fun resourceDirectories(buildType: String): Set<File> =
            android.sourceSets.getByName(buildType).res.srcDirs.map(File::getCanonicalFile).toSet()

        val releaseDirectories = resourceDirectories("release")
        val gateDirectories = resourceDirectories("gate")
        val migrationDirectories = resourceDirectories("migration")
        require(privateDirectory in releaseDirectories && migrationDirectory !in releaseDirectories)
        require(privateDirectory in gateDirectories && migrationDirectory !in gateDirectories)
        require(migrationDirectory in migrationDirectories && privateDirectory !in migrationDirectories) {
            "Relay trust resources are mapped to the wrong Android build type"
        }
    }
}

tasks.configureEach {
    if (name == "preBuild" && relayTrustBundleFilePath != null) {
        dependsOn(stageRelayTrustBundle)
    }
    if (name == "preReleaseBuild" || name == "preMigrationBuild" || name == "preGateBuild") {
        dependsOn(stageRelayTrustResources)
        dependsOn(verifyRelayTrustPolicyMapping)
        if (relayTrustBundleFilePath != null) dependsOn(stageRelayTrustBundle)
        doFirst {
            val bundle = relayTrustBundleFilePath?.let(::file)
                ?: relayTrustBundleDir?.let { file(it).resolve("relay-trust-bundle.json") }
            require(bundle?.isFile == true) {
                "Release requires -PrelayTrustBundleFile=<bundle> or a relayTrustBundleDir containing relay-trust-bundle.json"
            }
            val expectedPort = if (name == "preGateBuild") 27_462 else 27_461
            val publicBundle = requireNotNull(bundle).readText(Charsets.UTF_8)
            require(
                Regex(
                    """"publicIp"\s*:\s*"213\.250\.148\.166"""",
                ).containsMatchIn(publicBundle) &&
                    Regex(
                        """"servicePorts"\s*:\s*\[\s*$expectedPort\s*]""",
                    ).containsMatchIn(publicBundle)
            ) {
                "Relay trust bundle does not match this build's fixed IP and service port"
            }
        }
    }
    if (name == "preReleaseBuild" || name == "preMigrationBuild") {
        doFirst {
            require(
                releaseStoreFilePath != null &&
                    releaseStorePasswordFilePath != null &&
                    releaseStorePasswordValue != null &&
                    releaseKeyPasswordFilePath != null &&
                    releaseKeyPasswordValue != null
            ) {
                "Signed release requires a private PKCS12, two password files, and a key alias"
            }
            val signingFile = file(requireNotNull(releaseStoreFilePath)).canonicalFile
            val privateRoot = repositoryRoot.resolve("artifacts/private").canonicalFile
            val repositoryPath = repositoryRoot.canonicalFile.toPath()
            require(
                signingFile.isFile &&
                    (
                        signingFile.toPath().startsWith(privateRoot.toPath()) ||
                            !signingFile.toPath().startsWith(repositoryPath)
                        )
            ) {
                "Release keystore must be outside Git-tracked project paths"
            }
            listOf(
                requireNotNull(releaseStorePasswordFilePath),
                requireNotNull(releaseKeyPasswordFilePath),
            ).map(::file).forEach { secretFile ->
                require(secretFile.isFile) { "Release password file is missing" }
                val permissions = Files.getPosixFilePermissions(secretFile.toPath())
                require(
                    permissions.none {
                        it.name.startsWith("GROUP_") || it.name.startsWith("OTHERS_")
                    },
                ) { "Release password files must not be accessible by group or others" }
            }
        }
    }
}

androidComponents {
    onVariants(selector().withBuildType("migration")) { variant ->
        variant.outputs.forEach { output -> output.versionCode.set(6) }
    }
}

val verifyRelayApkSeparation = tasks.register("verifyRelayApkSeparation") {
    dependsOn("assembleMigration")
    mustRunAfter("assembleRelease")
    doLast {
        fun containsBytes(bytes: ByteArray, needle: ByteArray): Boolean =
            needle.isNotEmpty() &&
                bytes.size >= needle.size &&
                (0..bytes.size - needle.size).any { offset ->
                    needle.indices.all { index -> bytes[offset + index] == needle[index] }
                }

        fun apkContains(apk: File, marker: String): Boolean {
            val needle = marker.toByteArray(Charsets.US_ASCII)
            return ZipFile(apk).use { zip ->
                zip.entries().asSequence()
                    .filterNot { it.isDirectory }
                    .any { entry ->
                        zip.getInputStream(entry).use { containsBytes(it.readBytes(), needle) }
                    }
            }
        }

        val legacyOrigin =
            "https://chemicals-submission-capital-indianapolis.trycloudflare.com"
        val quickTunnelMarker = "trycloudflare.com"
        val releaseApk = layout.buildDirectory.file("outputs/apk/release/app-release.apk").get().asFile
        val migrationApk =
            layout.buildDirectory.file("outputs/apk/migration/app-migration.apk").get().asFile
        require(releaseApk.isFile) {
            "Final Relay APK must be assembled before its isolation gate"
        }
        require(!apkContains(releaseApk, quickTunnelMarker)) {
            "Final Relay APK must not contain any Quick Tunnel origin"
        }
        require(apkContains(migrationApk, legacyOrigin)) {
            "Migration APK must contain the exact authorized Quick Tunnel origin"
        }
    }
}

tasks.matching { it.name == "assembleRelease" }.configureEach {
    finalizedBy(verifyRelayApkSeparation)
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")

    implementation(project(":embedded:runtimeApi"))
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.exifinterface:exifinterface:1.3.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("com.squareup.okhttp3:okhttp-tls:4.12.0")

    androidTestImplementation(composeBom)
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:core-ktx:1.6.1")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    androidTestImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    androidTestImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    androidTestImplementation("com.squareup.okhttp3:okhttp-tls:4.12.0")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    add("gateImplementation", "androidx.compose.ui:ui-test-manifest")

}
