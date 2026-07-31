// miloco_launcher — generic signed launcher stub for macOS.
//
// Purpose: on macOS, Local Network Privacy (LNP) blocks a user-level (euid != 0)
// process from opening LAN sockets unless it has been granted the "Local Network"
// TCC permission. That grant is attributed to the *responsible process* of the
// connection. A bare interpreter (python) is a poor identity: the toggle shows up
// as "python" and grants every python on the machine. Instead we ship this tiny
// signed app bundle (CFBundleIdentifier=com.xiaomi.miloco.backend, bundle name "miloco")
// and spawn the real backend as its child WITHOUT disclaiming responsibility, so
// macOS attributes the child's local-network access to "miloco" — one clean,
// scoped toggle.
//
// This stub is intentionally generic and version-free: it just execs argv[1..] as
// a child. Keeping it free of any miloco-specific logic means the compiled binary
// is byte-stable across miloco releases, so its adhoc code-signature (cdhash) is
// stable and the one-time LNP grant persists across deploys.
//
// Contract:
//   miloco <program> [args...]
// It posix_spawns <program> with the given args, forwards SIGTERM/SIGINT to the
// child, and exits with the child's status (so launchd KeepAlive can restart the
// whole thing on crash). Environment is inherited (the LaunchAgent plist supplies
// MILOCO_HOME / MILOCO_SUPERVISED / PATH / HOME / TZ etc.).
//
// Build (on a Mac, once per arch; the signed .app is then vendored into the repo):
//   clang -arch arm64  -O2 -o Contents/MacOS/miloco miloco_launcher.c
//   clang -arch x86_64 -O2 -o Contents/MacOS/miloco miloco_launcher.c
//   codesign -s - --force --identifier com.xiaomi.miloco.backend <bundle>.app

#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

static volatile pid_t g_child = 0;

static void forward_signal(int sig) {
    if (g_child > 0) {
        kill(g_child, sig);
    }
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "miloco_launcher: usage: %s <program> [args...]\n", argv[0]);
        return 2;
    }

    // Forward the signals launchd uses to stop a job (SIGTERM) and Ctrl-C
    // (SIGINT, useful when run in the foreground for debugging) to the child.
    signal(SIGTERM, forward_signal);
    signal(SIGINT, forward_signal);

    // Block SIGTERM/SIGINT across the spawn so a signal delivered between
    // posix_spawn() and the g_child assignment isn't lost (the handler would
    // see g_child==0 and forward nothing). We restore the mask right after
    // g_child is set; a pending signal is then delivered and forwarded.
    // The *child* must NOT inherit this temporary block (uvicorn needs SIGTERM
    // for graceful shutdown), so spawn it with the original mask via
    // POSIX_SPAWN_SETSIGMASK.
    sigset_t block, orig;
    sigemptyset(&block);
    sigaddset(&block, SIGTERM);
    sigaddset(&block, SIGINT);
    sigprocmask(SIG_BLOCK, &block, &orig);

    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setsigmask(&attr, &orig);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK);

    pid_t child = 0;
    int rc = posix_spawn(&child, argv[1], NULL, &attr, &argv[1], environ);
    posix_spawnattr_destroy(&attr);
    if (rc != 0) {
        sigprocmask(SIG_SETMASK, &orig, NULL);
        fprintf(stderr, "miloco_launcher: posix_spawn(%s) failed: %s\n",
                argv[1], strerror(rc));
        return rc;
    }
    g_child = child;
    sigprocmask(SIG_SETMASK, &orig, NULL);

    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) {
            fprintf(stderr, "miloco_launcher: waitpid failed: %s\n", strerror(errno));
            return 1;
        }
    }

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
