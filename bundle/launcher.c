/* Graft launcher: exec the embedded Python on a module.
 *
 * Built twice by scripts/graft_bundle.py:
 *   Helpers/graft      -DMODULE='"graft_ipa"' -DPY_REL='"../Resources/GraftPython/bin/python3.13"'
 *   Helpers/bin/cyan   -DMODULE='"cyan"'      -DPY_REL='"../../Resources/GraftPython/bin/python3.13"'
 *
 * PY_REL is relative to the directory holding the launcher *after resolving
 * symlinks*, so ~/.local/bin/graft -> <app>/Contents/Helpers/graft works.
 * Compiled rather than a script: a signed script keeps its signature in
 * extended attributes, which a plain unzip strips.
 *
 * -I ignores PYTHON* variables and the user's site-packages; -B never writes
 * .pyc into the signed bundle. The Python is exec'd by its real in-bundle
 * path, which is how graft_ipa.tools finds Contents/Helpers/bin.
 */
#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef MODULE
#error define MODULE
#endif
#ifndef PY_REL
#error define PY_REL
#endif

/* The contract with the app (and anyone scripting graft): when the bundle
 * itself is broken, exit 126 with one line starting with this prefix. The
 * app shows it as "Graft.app is damaged — download it again". */
#define DAMAGED "graft: Graft.app is damaged: "
#define DAMAGED_EXIT 126

int main(int argc, char **argv) {
    char raw[PATH_MAX], self[PATH_MAX], py[PATH_MAX], pyreal[PATH_MAX];
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0 || !realpath(raw, self)) {
        fprintf(stderr, DAMAGED "its launcher can't find itself\n");
        return DAMAGED_EXIT;
    }
    char *slash = strrchr(self, '/');
    if (!slash) {
        fprintf(stderr, DAMAGED "its launcher is at %s\n", self);
        return DAMAGED_EXIT;
    }
    *slash = '\0';
    if (snprintf(py, sizeof py, "%s/%s", self, PY_REL) >= (int)sizeof py) {
        fprintf(stderr, DAMAGED "the path to its Python is too long\n");
        return DAMAGED_EXIT;
    }
    if (!realpath(py, pyreal)) {
        fprintf(stderr, DAMAGED "its Python is missing (%s)\n", py);
        return DAMAGED_EXIT;
    }
    /* -I ignores PYTHON* for everything but these two: on macOS CPython still
     * takes sys.executable from them, and graft finds the app's tools from
     * sys.executable (review #39, #42). */
    unsetenv("PYTHONEXECUTABLE");
    unsetenv("__PYVENV_LAUNCHER__");
    /* argc is 0 when started with an empty argv: there is nothing to pass on,
     * and the NULL still needs its slot (review #37). */
    int passed = argc > 1 ? argc - 1 : 0;
    char **nargv = calloc((size_t)passed + 6, sizeof *nargv);
    if (!nargv) {
        fprintf(stderr, "graft launcher: out of memory\n");
        return 1;
    }
    nargv[0] = pyreal;
    nargv[1] = "-I";
    nargv[2] = "-B";
    nargv[3] = "-m";
    nargv[4] = MODULE;
    for (int i = 0; i < passed; i++) nargv[5 + i] = argv[1 + i];
    execv(pyreal, nargv);
    fprintf(stderr, DAMAGED "can't run its Python (%s): %s\n", pyreal, strerror(errno));
    return DAMAGED_EXIT;
}
