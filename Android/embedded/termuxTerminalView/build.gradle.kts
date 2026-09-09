plugins {
    id("com.android.library")
}

val upstreamRoot = rootProject.projectDir.parentFile.resolve("third_party/termux-app")

android {
    namespace = "com.termux.view"
    compileSdk = 35

    defaultConfig {
        minSdk = 24
        targetSdk = 28
    }

    sourceSets {
        getByName("main") {
            manifest.srcFile(upstreamRoot.resolve("terminal-view/src/main/AndroidManifest.xml"))
            java.srcDir(upstreamRoot.resolve("terminal-view/src/main/java"))
            res.srcDir(upstreamRoot.resolve("terminal-view/src/main/res"))
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        abortOnError = true
        warningsAsErrors = false
    }
}

dependencies {
    api(project(":embedded:termuxTerminalEmulator"))
    implementation("androidx.annotation:annotation:1.3.0")
    testImplementation("junit:junit:4.13.2")
}
