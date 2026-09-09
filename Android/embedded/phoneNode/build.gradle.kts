plugins {
    id("com.android.library")
}

val existingPhoneNode = rootProject.projectDir.resolve("phoneNode")

android {
    namespace = "com.chebysight.chebyagent.android"
    compileSdk = 35

    defaultConfig {
        minSdk = 28
        buildConfigField("String", "VERSION_NAME", "\"0.5.0-embedded-dev\"")
        buildConfigField("boolean", "PHONEBRIDGE_ENABLED", "true")
        buildConfigField("boolean", "STANDALONE_MODE", "true")
        buildConfigField("String", "AGENT_NODE_PRODUCT_NAME", "\"ChebyNode Embedded\"")
        buildConfigField("String", "PHONEBRIDGE_DEFAULT_BASE_URL", "\"http://127.0.0.1:3448\"")
    }

    sourceSets {
        getByName("main") {
            manifest.srcFile("src/main/AndroidManifest.xml")
            java.srcDir(existingPhoneNode.resolve("src/main/java"))
            res.srcDir(existingPhoneNode.resolve("src/main/res"))
        }
        getByName("debug").res.srcDir(existingPhoneNode.resolve("src/standalone/res"))
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
        warningsAsErrors = false
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}
