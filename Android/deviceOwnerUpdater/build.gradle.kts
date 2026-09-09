plugins {
    id("com.android.application")
}

val allowedTargetSignerSha256 = providers.gradleProperty("chebyUpdaterTargetSignerSha256")
    .orElse("dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730")

android {
    namespace = "com.cheby.codex.updater"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.cheby.codex.updater"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        buildConfigField("String", "TARGET_PACKAGE", "\"com.termux\"")
        buildConfigField(
            "String",
            "TARGET_SIGNER_SHA256",
            "\"${allowedTargetSignerSha256.get()}\"",
        )
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("debug")
        }
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }

    lint {
        abortOnError = true
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}
