plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

val existingApp = rootProject.projectDir.resolve("app")

android {
    namespace = "com.cheby.codex.mobile"
    compileSdk = 35

    defaultConfig {
        minSdk = 28
        buildConfigField("String", "APPLICATION_ID", "\"com.termux\"")
        buildConfigField("String", "VERSION_NAME", "\"0.5.0-embedded-dev\"")
        buildConfigField("boolean", "STANDALONE_MODE", "true")
        buildConfigField("boolean", "DEMO_GATEWAY_ENABLED", "false")
        buildConfigField("boolean", "RELAY_PRIVATE_CA_REQUIRED", "false")
        buildConfigField("boolean", "RELAY_PLATFORM_TRUST_FALLBACK", "true")
        buildConfigField("boolean", "LEGACY_RELAY_MIGRATION_ENABLED", "false")
        buildConfigField("String", "LEGACY_RELAY_ORIGIN", "\"\"")
        buildConfigField("int", "RELAY_SERVICE_PORT", "27461")
    }

    sourceSets {
        getByName("main") {
            manifest.srcFile("src/main/AndroidManifest.xml")
            kotlin.srcDir(existingApp.resolve("src/main/java"))
            res.srcDir(existingApp.resolve("src/main/res"))
        }
        getByName("debug").res.srcDir(existingApp.resolve("src/standalone/res"))
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

    lint {
        abortOnError = true
        warningsAsErrors = false
    }
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
    debugImplementation("androidx.compose.ui:ui-tooling")
}
