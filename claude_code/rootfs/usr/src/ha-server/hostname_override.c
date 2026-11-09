/*
 * Report a chosen hostname to one process tree.
 *
 * Claude Code's Remote Control server registers the machine with the
 * user's Claude account under the machine's hostname. Inside a Home
 * Assistant app that hostname is set by the Supervisor ("<repo>-<slug>",
 * e.g. "beb500c8-claude-code") and an app has no way to change it.
 *
 * Loaded with LD_PRELOAD into the `claude remote-control` process only,
 * this library makes gethostname() and uname() report the value of
 * HA_SERVER_HOSTNAME instead. When the variable is unset or empty, both
 * calls behave exactly as normal. Nothing else in the container is
 * affected: the boot script never sets LD_PRELOAD globally.
 *
 * The server spawns the sessions started from the Claude app as child
 * processes, and children inherit the environment. So at load time the
 * library reads HA_SERVER_HOSTNAME once, then removes it and its own entry
 * in LD_PRELOAD from the environment. The server keeps the override, while
 * its sessions, their shells and tools see the real hostname.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/utsname.h>
#include <unistd.h>

#define LIB_NAME "hostname_override.so"

static char override_buf[256];

static const char *override_name(void)
{
    return override_buf[0] != '\0' ? override_buf : NULL;
}

/* True when an LD_PRELOAD entry names this library. */
static int is_this_lib(const char *entry, size_t len)
{
    size_t n = strlen(LIB_NAME);
    return len >= n && memcmp(entry + len - n, LIB_NAME, n) == 0
        && (len == n || entry[len - n - 1] == '/');
}

/* Rewrite LD_PRELOAD without this library, keeping any other entries. */
static void drop_self_from_preload(void)
{
    const char *preload = getenv("LD_PRELOAD");
    if (preload == NULL)
        return;

    size_t total = strlen(preload);
    char *kept = malloc(total + 1);
    if (kept == NULL) {
        unsetenv("LD_PRELOAD");
        return;
    }

    size_t out = 0;
    const char *p = preload;
    while (*p != '\0') {
        size_t len = strcspn(p, " :");
        if (len > 0 && !is_this_lib(p, len)) {
            if (out > 0)
                kept[out++] = ' ';
            memcpy(kept + out, p, len);
            out += len;
        }
        p += len;
        if (*p != '\0')
            p++;
    }
    kept[out] = '\0';

    if (out == 0)
        unsetenv("LD_PRELOAD");
    else
        setenv("LD_PRELOAD", kept, 1);
    free(kept);
}

__attribute__((constructor))
static void ha_server_init(void)
{
    const char *name = getenv("HA_SERVER_HOSTNAME");
    if (name != NULL)
        snprintf(override_buf, sizeof(override_buf), "%s", name);
    unsetenv("HA_SERVER_HOSTNAME");
    drop_self_from_preload();
}

int uname(struct utsname *buf)
{
    static int (*real_uname)(struct utsname *);
    if (real_uname == NULL)
        real_uname = (int (*)(struct utsname *))dlsym(RTLD_NEXT, "uname");
    if (real_uname == NULL) {
        errno = ENOSYS;
        return -1;
    }

    int rc = real_uname(buf);
    const char *name = override_name();
    if (rc == 0 && name != NULL) {
        strncpy(buf->nodename, name, sizeof(buf->nodename) - 1);
        buf->nodename[sizeof(buf->nodename) - 1] = '\0';
    }
    return rc;
}

int gethostname(char *out, size_t len)
{
    const char *name = override_name();
    if (name == NULL) {
        static int (*real_gethostname)(char *, size_t);
        if (real_gethostname == NULL)
            real_gethostname = (int (*)(char *, size_t))dlsym(RTLD_NEXT, "gethostname");
        if (real_gethostname == NULL) {
            errno = ENOSYS;
            return -1;
        }
        return real_gethostname(out, len);
    }

    size_t n = strlen(name);
    if (n >= len) {
        errno = ENAMETOOLONG;
        return -1;
    }
    memcpy(out, name, n + 1);
    return 0;
}
