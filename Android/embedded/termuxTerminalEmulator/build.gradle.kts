plugins {
    id("com.android.library")
}

val upstreamRoot = rootProject.projectDir.parentFile.resolve("third_party/termux-app")

android {
    namespace = "com.termux.terminal"
    compileSdk = 35
    ndkVersion = "27.2.12479018"

    defaultConfig {
        minSdk = 24
        targetSdk = 28
        ndk {
            abiFilters += "arm64-v8a"
        }
    }

    sourceSets {
        getByName("main") {
            manifest.srcFile(upstreamRoot.resolve("terminal-emulator/src/main/AndroidManifest.xml"))
            java.srcDir(upstreamRoot.resolve("terminal-emulator/src/main/java"))
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    externalNativeBuild {
        ndkBuild {
            path = upstreamRoot.resolve("terminal-emulator/src/main/jni/Android.mk")
        }
    }

    lint {
        abortOnError = true
        warningsAsErrors = false
    }
}

dependencies {
    implementation("androidx.annotation:annotation:1.3.0")
    testImplementation("junit:junit:4.13.2")
}
