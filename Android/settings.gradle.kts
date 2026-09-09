pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "ChebyCodexMobile"
include(":app")
include(":phoneNode")
include(":embedded:termuxTerminalEmulator")
include(":embedded:termuxTerminalView")
include(":embedded:termuxShared")
include(":embedded:chebyUi")
include(":embedded:phoneNode")
include(":embedded:runtimeApi")
include(":appliance")
include(":testIme")
include(":deviceOwnerUpdater")
