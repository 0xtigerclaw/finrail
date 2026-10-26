/* finrail-launcher — the first process of every finrail service under launchd.
 *
 * It does one thing: start the real command as a CHILD, stay its parent, pass on
 * the signals launchd sends, and exit the way the child did. See __init__.py in
 * this directory for why a program this small has to exist at all.
 *
 * NEVER exec. macOS attributes a process's file access to the first non-Apple
 * process that launchd started for the job, and only if that process is still
 * the root of the tree. Replacing this image with the interpreter would hand the
 * question straight back to the interpreter's versioned path — which is the
 * outage this exists to prevent.
 */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

static volatile sig_atomic_t child = 0;

/* The signals launchd or a person might send to stop, reload or poke a service.
 * Forwarded rather than acted on: the chat harness, for one, finishes the turn
 * in flight on SIGTERM before it exits, and a launcher that simply died would
 * take the whole process group down under it mid-answer. */
static const int FORWARDED[] = {SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1, SIGUSR2};
#define NFORWARDED (sizeof FORWARDED / sizeof FORWARDED[0])

static void forward(int sig) {
    if (child > 0) {
        kill(child, sig);
    }
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "finrail-launcher: nothing to run "
                        "(usage: finrail-launcher <program> [args...])\n");
        return 64; /* EX_USAGE */
    }

    /* Block the forwarded signals until the child exists. A stop that arrived in
     * the gap between installing the handler and knowing the child's pid would
     * otherwise be delivered to nobody — and the service would carry on. */
    sigset_t forwarded, previous;
    sigemptyset(&forwarded);
    for (size_t i = 0; i < NFORWARDED; i++) {
        sigaddset(&forwarded, FORWARDED[i]);
    }
    sigprocmask(SIG_BLOCK, &forwarded, &previous);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_RESTART;
    for (size_t i = 0; i < NFORWARDED; i++) {
        sigaction(FORWARDED[i], &sa, NULL);
    }

    /* The child must not inherit the blocked mask, or it would never hear a stop:
     * it starts with an empty mask and default dispositions, as if launchd had
     * started it directly. */
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    sigset_t empty, defaults;
    sigemptyset(&empty);
    sigfillset(&defaults);
    posix_spawnattr_setsigmask(&attr, &empty);
    posix_spawnattr_setsigdefault(&attr, &defaults);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);

    pid_t pid;
    int rc = posix_spawnp(&pid, argv[1], NULL, &attr, &argv[1], environ);
    posix_spawnattr_destroy(&attr);
    if (rc != 0) {
        fprintf(stderr, "finrail-launcher: cannot start %s: %s\n", argv[1], strerror(rc));
        return 127; /* the shell's "command not found", which is what this is */
    }
    child = pid;
    sigprocmask(SIG_SETMASK, &previous, NULL); /* anything pending is forwarded now */

    int status;
    for (;;) {
        pid_t w = waitpid(pid, &status, 0);
        if (w == pid) {
            break;
        }
        if (w < 0 && errno == EINTR) {
            continue;
        }
        perror("finrail-launcher: waitpid");
        return 70; /* EX_SOFTWARE */
    }

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        /* Die the same way, so launchd's view of the job — KeepAlive, crash
         * accounting, `launchctl print` — is the child's and not a stand-in's. */
        int sig = WTERMSIG(status);
        signal(sig, SIG_DFL);
        sigset_t just;
        sigemptyset(&just);
        sigaddset(&just, sig);
        sigprocmask(SIG_UNBLOCK, &just, NULL);
        raise(sig);
        return 128 + sig;
    }
    return 1;
}
