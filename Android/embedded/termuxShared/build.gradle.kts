plugins {
    id("com.android.library")
}

val upstreamRoot = rootProject.projectDir.parentFile.resolve("third_party/termux-app")

android {
    namespace = "com.termux.shared"
    compileSdk = 35

    defaultConfig {
        minSdk = 24
        targetSdk = 28
    }

    sourceSets {
        getByName("main") {
            manifest.srcFile(upstreamRoot.resolve("termux-shared/src/main/AndroidManifest.xml"))
            java.srcDir(upstreamRoot.resolve("termux-shared/src/main/java"))
            res.srcDir(upstreamRoot.resolve("termux-shared/src/main/res"))
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
    implementation(project(":embedded:termuxTerminalView"))
    implementation("androidx.appcompat:appcompat:1.3.1")
    implementation("androidx.annotation:annotation:1.3.0")
    implementation("androidx.core:core:1.6.0")
    implementation("com.google.android.material:material:1.4.0")
    implementation("com.google.guava:guava:24.1-jre")
    implementation("io.noties.markwon:core:4.6.2")
    implementation("io.noties.markwon:ext-strikethrough:4.6.2")
    implementation("io.noties.markwon:linkify:4.6.2")
    implementation("io.noties.markwon:recycler:4.6.2")
    implementation("org.lsposed.hiddenapibypass:hiddenapibypass:6.1")
    implementation("androidx.window:window:1.0.0-alpha09")
    implementation("commons-io:commons-io:2.5")
    testImplementation("junit:junit:4.13.2")
}
