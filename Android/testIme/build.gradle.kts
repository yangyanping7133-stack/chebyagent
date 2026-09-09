plugins {
    id("com.android.application")
}

android {
    namespace = "com.cheby.testime"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.cheby.testime"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
