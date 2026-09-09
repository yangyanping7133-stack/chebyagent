plugins {
    id("com.android.application")
}

android {
    namespace = "com.chebysight.chebyagent.android"
    compileSdk = 35

    buildFeatures {
        buildConfig = true
    }

    defaultConfig {
        applicationId = "com.chebysight.chebyagent.phonenode"
        minSdk = 28
        targetSdk = 35
        versionCode = 37
        versionName = "0.4.11-phonebridge"
        buildConfigField("boolean", "PHONEBRIDGE_ENABLED", "true")
        buildConfigField("boolean", "STANDALONE_MODE", "false")
        buildConfigField("String", "AGENT_NODE_PRODUCT_NAME", "\"ChebyNode\"")
        buildConfigField(
            "String",
            "PHONEBRIDGE_DEFAULT_BASE_URL",
            "\"https://mcp.chebyshev.net/phonebridge\"",
        )
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("debug")
        }
        create("standalone") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".standalone"
            versionNameSuffix = "-standalone"
            buildConfigField("boolean", "STANDALONE_MODE", "true")
            buildConfigField(
                "String",
                "AGENT_NODE_PRODUCT_NAME",
                "\"ChebyNode Standalone\"",
            )
            buildConfigField(
                "String",
                "PHONEBRIDGE_DEFAULT_BASE_URL",
                "\"http://127.0.0.1:3448\"",
            )
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        abortOnError = true
        checkReleaseBuilds = true
        warningsAsErrors = false
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
