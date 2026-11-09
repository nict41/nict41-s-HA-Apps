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
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/utsname.h>
#include <unistd.h>

static const char *override_name(void)
{
    const char *name = getenv("HA_SERVER_HOSTNAME");
    return (name != NULL && name[0] != '\0') ? name : NULL;
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
