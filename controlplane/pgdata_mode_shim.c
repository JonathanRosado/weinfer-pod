#define _GNU_SOURCE
#define _LARGEFILE64_SOURCE

#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

/*
 * RunPod Network Volumes expose uniform permission bits: chmod(0700)
 * succeeds but a following stat still reports the data directory as
 * world-accessible. PostgreSQL intentionally refuses such a data directory.
 *
 * This preload library changes only stat-family results for the ONE exact
 * WEINFER_PGDATA directory, and only in initdb/postgres processes into which
 * the entrypoint explicitly loads it. All file I/O still goes directly to the
 * durable volume; no pathname is redirected and the gateway never loads this
 * library.
 */

static const char *pgdata_root(void) {
    const char *root = getenv("WEINFER_PGDATA");
    return (root != NULL && root[0] != '\0') ? root : "/workspace/pgdata";
}

static size_t trimmed_length(const char *path) {
    size_t length = strlen(path);
    while (length > 1 && path[length - 1] == '/') {
        length--;
    }
    return length;
}

static bool is_pgdata_path(const char *path) {
    if (path == NULL) {
        return false;
    }
    const char *root = pgdata_root();
    if (strcmp(path, ".") == 0) {
        char current[PATH_MAX + 1];
        return getcwd(current, sizeof(current)) != NULL && is_pgdata_path(current);
    }
    const size_t path_length = trimmed_length(path);
    const size_t root_length = trimmed_length(root);
    return path_length == root_length && strncmp(path, root, root_length) == 0;
}

static bool fd_is_pgdata(int fd) {
    char link_path[64];
    char target[PATH_MAX + 1];
    const int written = snprintf(link_path, sizeof(link_path), "/proc/self/fd/%d", fd);
    if (written < 0 || (size_t)written >= sizeof(link_path)) {
        return false;
    }
    const ssize_t length = readlink(link_path, target, PATH_MAX);
    if (length < 0) {
        return false;
    }
    target[length] = '\0';
    return is_pgdata_path(target);
}

static bool at_path_is_pgdata(int dirfd, const char *path) {
    if (path == NULL) {
        return false;
    }
    if (path[0] == '/') {
        return is_pgdata_path(path);
    }

    char base[PATH_MAX + 1];
    if (dirfd == AT_FDCWD) {
        if (getcwd(base, sizeof(base)) == NULL) {
            return false;
        }
    } else {
        char link_path[64];
        const int written = snprintf(link_path, sizeof(link_path), "/proc/self/fd/%d", dirfd);
        if (written < 0 || (size_t)written >= sizeof(link_path)) {
            return false;
        }
        const ssize_t length = readlink(link_path, base, PATH_MAX);
        if (length < 0) {
            return false;
        }
        base[length] = '\0';
    }

    char joined[PATH_MAX + 1];
    const int written = snprintf(joined, sizeof(joined), "%s/%s", base, path);
    return written >= 0 && (size_t)written < sizeof(joined) && is_pgdata_path(joined);
}

static void mask_stat(struct stat *buffer) {
    if (buffer == NULL || !S_ISDIR(buffer->st_mode)) {
        return;
    }
    buffer->st_mode = (buffer->st_mode & ~(mode_t)0777) | (mode_t)0700;
    buffer->st_uid = geteuid();
    buffer->st_gid = getegid();
}

static void mask_stat64(struct stat64 *buffer) {
    if (buffer == NULL || !S_ISDIR(buffer->st_mode)) {
        return;
    }
    buffer->st_mode = (buffer->st_mode & ~(mode_t)0777) | (mode_t)0700;
    buffer->st_uid = geteuid();
    buffer->st_gid = getegid();
}

#define RESOLVE(symbol, type)                                                                    \
    static type real_##symbol = NULL;                                                            \
    if (real_##symbol == NULL) {                                                                 \
        real_##symbol = (type)dlsym(RTLD_NEXT, #symbol);                                         \
        if (real_##symbol == NULL) {                                                             \
            _exit(127);                                                                          \
        }                                                                                        \
    }

int stat(const char *path, struct stat *buffer) {
    typedef int (*function_type)(const char *, struct stat *);
    RESOLVE(stat, function_type);
    const int result = real_stat(path, buffer);
    if (result == 0 && is_pgdata_path(path)) {
        mask_stat(buffer);
    }
    return result;
}

int lstat(const char *path, struct stat *buffer) {
    typedef int (*function_type)(const char *, struct stat *);
    RESOLVE(lstat, function_type);
    const int result = real_lstat(path, buffer);
    if (result == 0 && is_pgdata_path(path)) {
        mask_stat(buffer);
    }
    return result;
}

int fstat(int fd, struct stat *buffer) {
    typedef int (*function_type)(int, struct stat *);
    RESOLVE(fstat, function_type);
    const int result = real_fstat(fd, buffer);
    if (result == 0 && fd_is_pgdata(fd)) {
        mask_stat(buffer);
    }
    return result;
}

int fstatat(int dirfd, const char *path, struct stat *buffer, int flags) {
    typedef int (*function_type)(int, const char *, struct stat *, int);
    RESOLVE(fstatat, function_type);
    const int result = real_fstatat(dirfd, path, buffer, flags);
    if (result == 0 && at_path_is_pgdata(dirfd, path)) {
        mask_stat(buffer);
    }
    return result;
}

int stat64(const char *path, struct stat64 *buffer) {
    typedef int (*function_type)(const char *, struct stat64 *);
    RESOLVE(stat64, function_type);
    const int result = real_stat64(path, buffer);
    if (result == 0 && is_pgdata_path(path)) {
        mask_stat64(buffer);
    }
    return result;
}

int lstat64(const char *path, struct stat64 *buffer) {
    typedef int (*function_type)(const char *, struct stat64 *);
    RESOLVE(lstat64, function_type);
    const int result = real_lstat64(path, buffer);
    if (result == 0 && is_pgdata_path(path)) {
        mask_stat64(buffer);
    }
    return result;
}

int fstat64(int fd, struct stat64 *buffer) {
    typedef int (*function_type)(int, struct stat64 *);
    RESOLVE(fstat64, function_type);
    const int result = real_fstat64(fd, buffer);
    if (result == 0 && fd_is_pgdata(fd)) {
        mask_stat64(buffer);
    }
    return result;
}

int fstatat64(int dirfd, const char *path, struct stat64 *buffer, int flags) {
    typedef int (*function_type)(int, const char *, struct stat64 *, int);
    RESOLVE(fstatat64, function_type);
    const int result = real_fstatat64(dirfd, path, buffer, flags);
    if (result == 0 && at_path_is_pgdata(dirfd, path)) {
        mask_stat64(buffer);
    }
    return result;
}

int __xstat(int version, const char *path, struct stat *buffer) {
    typedef int (*function_type)(int, const char *, struct stat *);
    RESOLVE(__xstat, function_type);
    const int result = real___xstat(version, path, buffer);
    if (result == 0 && is_pgdata_path(path)) {
        mask_stat(buffer);
    }
    return result;
}

int __lxstat(int version, const char *path, struct stat *buffer) {
    typedef int (*function_type)(int, const char *, struct stat *);
    RESOLVE(__lxstat, function_type);
    const int result = real___lxstat(version, path, buffer);
    if (result == 0 && is_pgdata_path(path)) {
        mask_stat(buffer);
    }
    return result;
}

int __fxstat(int version, int fd, struct stat *buffer) {
    typedef int (*function_type)(int, int, struct stat *);
    RESOLVE(__fxstat, function_type);
    const int result = real___fxstat(version, fd, buffer);
    if (result == 0 && fd_is_pgdata(fd)) {
        mask_stat(buffer);
    }
    return result;
}

int __fxstatat(int version, int dirfd, const char *path, struct stat *buffer, int flags) {
    typedef int (*function_type)(int, int, const char *, struct stat *, int);
    RESOLVE(__fxstatat, function_type);
    const int result = real___fxstatat(version, dirfd, path, buffer, flags);
    if (result == 0 && at_path_is_pgdata(dirfd, path)) {
        mask_stat(buffer);
    }
    return result;
}
