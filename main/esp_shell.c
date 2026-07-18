#include "esp_shell.h"
#include "agent_pack.h"

#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_vfs_fat.h"
#include "esp_wifi.h"
#include "wear_levelling.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/unistd.h>
#include <time.h>

#define FS_ROOT "/fs"
#define FS_LABEL "storage"
#define PATH_MAX_LOCAL 256
#define COMMAND_MAX 512
#define MAX_ARGS 20

static const char *TAG = "esp_shell";
static bool s_ready;
static wl_handle_t s_wl_handle = WL_INVALID_HANDLE;

static bool append_text(char **cursor, size_t *remaining, const char *text)
{
    size_t len;
    if (!cursor || !*cursor || !remaining || *remaining == 0 || !text) {
        return false;
    }
    len = strlen(text);
    if (len >= *remaining) {
        len = *remaining - 1;
    }
    memcpy(*cursor, text, len);
    *cursor += len;
    *remaining -= len;
    **cursor = '\0';
    return text[len] == '\0';
}

static bool append_fmt(char **cursor, size_t *remaining, const char *fmt, ...)
{
    va_list args;
    int written;
    if (!cursor || !*cursor || !remaining || *remaining == 0) {
        return false;
    }
    va_start(args, fmt);
    written = vsnprintf(*cursor, *remaining, fmt, args);
    va_end(args);
    if (written < 0) {
        return false;
    }
    if ((size_t)written >= *remaining) {
        *cursor += *remaining - 1;
        *remaining = 1;
        return false;
    }
    *cursor += written;
    *remaining -= (size_t)written;
    return true;
}

static bool normalize_path(const char *cwd, const char *input,
                           char *virtual_path, size_t virtual_len)
{
    char combined[PATH_MAX_LOCAL * 2];
    char work[PATH_MAX_LOCAL * 2];
    char *parts[48];
    char *save = NULL;
    char *token;
    int count = 0;
    size_t used = 1;

    if (!cwd || cwd[0] != '/') {
        cwd = ESP_SHELL_HOME;
    }
    if (!input || input[0] == '\0') {
        input = cwd;
    }
    if (strcmp(input, "~") == 0) {
        input = ESP_SHELL_HOME;
    }

    if (input[0] == '/') {
        snprintf(combined, sizeof(combined), "%s", input);
    } else if (strncmp(input, "~/", 2) == 0) {
        snprintf(combined, sizeof(combined), "%s/%s", ESP_SHELL_HOME, input + 2);
    } else {
        snprintf(combined, sizeof(combined), "%s/%s", cwd, input);
    }

    if (strlen(combined) >= sizeof(work)) {
        return false;
    }
    strcpy(work, combined);
    token = strtok_r(work, "/", &save);
    while (token) {
        if (strcmp(token, ".") == 0 || token[0] == '\0') {
            /* Skip. */
        } else if (strcmp(token, "..") == 0) {
            if (count > 0) {
                count--;
            }
        } else {
            if (count >= (int)(sizeof(parts) / sizeof(parts[0]))) {
                return false;
            }
            parts[count++] = token;
        }
        token = strtok_r(NULL, "/", &save);
    }

    if (virtual_len < 2) {
        return false;
    }
    virtual_path[0] = '/';
    virtual_path[1] = '\0';
    for (int i = 0; i < count; i++) {
        size_t part_len = strlen(parts[i]);
        if (used + part_len + (used > 1 ? 1 : 0) >= virtual_len) {
            return false;
        }
        if (used > 1) {
            virtual_path[used++] = '/';
        }
        memcpy(virtual_path + used, parts[i], part_len);
        used += part_len;
        virtual_path[used] = '\0';
    }
    return true;
}

static bool resolve_path(const char *cwd, const char *input,
                         char *virtual_path, size_t virtual_len,
                         char *actual_path, size_t actual_len)
{
    if (!normalize_path(cwd, input, virtual_path, virtual_len)) {
        return false;
    }
    return snprintf(actual_path, actual_len, FS_ROOT "%s", virtual_path) < (int)actual_len;
}

static bool ensure_ready(char *result, size_t result_len)
{
    esp_err_t err = esp_shell_init();
    if (err == ESP_OK) {
        return true;
    }
    if (result && result_len) {
        snprintf(result, result_len, "Error: filesystem unavailable (%s)", esp_err_to_name(err));
    }
    return false;
}

esp_err_t esp_shell_init(void)
{
    if (s_ready) {
        return ESP_OK;
    }

    const esp_vfs_fat_mount_config_t mount_config = {
        .format_if_mount_failed = true,
        .max_files = 6,
        .allocation_unit_size = 4096,
    };
    esp_err_t err = esp_vfs_fat_spiflash_mount_rw_wl(
        FS_ROOT, FS_LABEL, &mount_config, &s_wl_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "FAT filesystem mount failed: %s", esp_err_to_name(err));
        return err;
    }

    mkdir(FS_ROOT "/home", 0755);
    mkdir(FS_ROOT ESP_SHELL_HOME, 0755);
    mkdir(FS_ROOT "/tmp", 0755);

    FILE *welcome = fopen(FS_ROOT ESP_SHELL_HOME "/README.txt", "r");
    if (welcome) {
        fclose(welcome);
    } else {
        welcome = fopen(FS_ROOT ESP_SHELL_HOME "/README.txt", "w");
        if (welcome) {
            fputs("zclaw ESP filesystem\nUse !help locally or let the agent use shell_exec.\n", welcome);
            fclose(welcome);
        }
    }

    s_ready = true;
    ESP_LOGI(TAG, "Filesystem mounted at %s", FS_ROOT);
    return ESP_OK;
}

static bool list_at(const char *cwd, const char *path, char *result, size_t result_len)
{
    char virt[PATH_MAX_LOCAL];
    char actual[PATH_MAX_LOCAL + 8];
    char *cursor = result;
    size_t remaining = result_len;
    struct stat st;

    if (!ensure_ready(result, result_len)) {
        return false;
    }
    if (!resolve_path(cwd, path, virt, sizeof(virt), actual, sizeof(actual))) {
        snprintf(result, result_len, "Error: path too long");
        return false;
    }
    if (stat(actual, &st) != 0) {
        snprintf(result, result_len, "ls: %s: %s", path ? path : ".", strerror(errno));
        return false;
    }
    if (!S_ISDIR(st.st_mode)) {
        snprintf(result, result_len, "%8ld  %s", (long)st.st_size, virt);
        return true;
    }

    DIR *dir = opendir(actual);
    if (!dir) {
        snprintf(result, result_len, "ls: %s: %s", virt, strerror(errno));
        return false;
    }
    result[0] = '\0';
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        char child[(PATH_MAX_LOCAL * 2) + 32];
        struct stat child_st;
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue;
        }
        snprintf(child, sizeof(child), "%s/%s", actual, entry->d_name);
        if (stat(child, &child_st) == 0) {
            append_fmt(&cursor, &remaining, "%c %8ld  %s\n",
                       S_ISDIR(child_st.st_mode) ? 'd' : '-',
                       (long)child_st.st_size, entry->d_name);
        } else {
            append_fmt(&cursor, &remaining, "?          %s\n", entry->d_name);
        }
        if (remaining <= 2) {
            break;
        }
    }
    closedir(dir);
    if (result[0] == '\0') {
        snprintf(result, result_len, "(empty)");
    }
    return true;
}

bool esp_shell_list(const char *path, char *result, size_t result_len)
{
    return list_at(ESP_SHELL_HOME, path, result, result_len);
}

static bool read_at(const char *cwd, const char *path, char *result, size_t result_len)
{
    char virt[PATH_MAX_LOCAL];
    char actual[PATH_MAX_LOCAL + 8];
    size_t n;
    if (!ensure_ready(result, result_len)) {
        return false;
    }
    if (!path || !resolve_path(cwd, path, virt, sizeof(virt), actual, sizeof(actual))) {
        snprintf(result, result_len, "Error: invalid path");
        return false;
    }
    FILE *file = fopen(actual, "rb");
    if (!file) {
        snprintf(result, result_len, "cat: %s: %s", virt, strerror(errno));
        return false;
    }
    n = fread(result, 1, result_len - 1, file);
    result[n] = '\0';
    if (!feof(file) && n > 16) {
        memcpy(result + n - 15, "\n[truncated]\n", 14);
        result[n - 1] = '\0';
    }
    fclose(file);
    return true;
}

bool esp_shell_read(const char *path, char *result, size_t result_len)
{
    return read_at(ESP_SHELL_HOME, path, result, result_len);
}

static bool write_at(const char *cwd, const char *path, const char *content,
                     bool append, char *result, size_t result_len)
{
    char virt[PATH_MAX_LOCAL];
    char actual[PATH_MAX_LOCAL + 8];
    size_t content_len;
    if (!ensure_ready(result, result_len)) {
        return false;
    }
    if (!path || !content || !resolve_path(cwd, path, virt, sizeof(virt), actual, sizeof(actual))) {
        snprintf(result, result_len, "Error: invalid path or content");
        return false;
    }
    content_len = strlen(content);
    if (content_len > 4096) {
        snprintf(result, result_len, "Error: one write is limited to 4096 bytes");
        return false;
    }
    FILE *file = fopen(actual, append ? "ab" : "wb");
    if (!file) {
        snprintf(result, result_len, "write: %s: %s", virt, strerror(errno));
        return false;
    }
    size_t written = fwrite(content, 1, content_len, file);
    fclose(file);
    if (written != content_len) {
        snprintf(result, result_len, "write: %s: incomplete write", virt);
        return false;
    }
    snprintf(result, result_len, "%s %zu bytes to %s",
             append ? "Appended" : "Wrote", written, virt);
    return true;
}

bool esp_shell_write(const char *path, const char *content, bool append,
                     char *result, size_t result_len)
{
    return write_at(ESP_SHELL_HOME, path, content, append, result, result_len);
}

static bool remove_at(const char *cwd, const char *path, char *result, size_t result_len)
{
    char virt[PATH_MAX_LOCAL];
    char actual[PATH_MAX_LOCAL + 8];
    struct stat st;
    int rc;
    if (!ensure_ready(result, result_len)) {
        return false;
    }
    if (!path || !resolve_path(cwd, path, virt, sizeof(virt), actual, sizeof(actual)) || strcmp(virt, "/") == 0) {
        snprintf(result, result_len, "Error: refusing invalid/root path");
        return false;
    }
    if (stat(actual, &st) != 0) {
        snprintf(result, result_len, "rm: %s: %s", virt, strerror(errno));
        return false;
    }
    rc = S_ISDIR(st.st_mode) ? rmdir(actual) : unlink(actual);
    if (rc != 0) {
        snprintf(result, result_len, "rm: %s: %s", virt, strerror(errno));
        return false;
    }
    snprintf(result, result_len, "Removed %s", virt);
    return true;
}

bool esp_shell_remove(const char *path, char *result, size_t result_len)
{
    return remove_at(ESP_SHELL_HOME, path, result, result_len);
}

static int split_args(char *line, char **argv, int max_args)
{
    int argc = 0;
    char *p = line;
    while (*p && argc < max_args) {
        char quote = 0;
        while (isspace((unsigned char)*p)) p++;
        if (!*p) break;
        if (*p == '\'' || *p == '"') {
            quote = *p++;
        }
        argv[argc++] = p;
        while (*p && ((quote && *p != quote) || (!quote && !isspace((unsigned char)*p)))) p++;
        if (*p) *p++ = '\0';
    }
    return argc;
}

static bool copy_file_at(const char *cwd, const char *source, const char *dest,
                         char *result, size_t result_len)
{
    char sv[PATH_MAX_LOCAL], sa[PATH_MAX_LOCAL + 8];
    char dv[PATH_MAX_LOCAL], da[PATH_MAX_LOCAL + 8];
    char buffer[512];
    size_t n;
    if (!resolve_path(cwd, source, sv, sizeof(sv), sa, sizeof(sa)) ||
        !resolve_path(cwd, dest, dv, sizeof(dv), da, sizeof(da))) {
        snprintf(result, result_len, "cp: invalid path");
        return false;
    }
    FILE *in = fopen(sa, "rb");
    if (!in) {
        snprintf(result, result_len, "cp: %s: %s", sv, strerror(errno));
        return false;
    }
    FILE *out = fopen(da, "wb");
    if (!out) {
        fclose(in);
        snprintf(result, result_len, "cp: %s: %s", dv, strerror(errno));
        return false;
    }
    bool ok = true;
    while ((n = fread(buffer, 1, sizeof(buffer), in)) > 0) {
        if (fwrite(buffer, 1, n, out) != n) {
            ok = false;
            break;
        }
    }
    fclose(in);
    fclose(out);
    snprintf(result, result_len, ok ? "Copied %s to %s" : "cp: write failed", sv, dv);
    return ok;
}

bool esp_shell_execute(const char *command, char *cwd, size_t cwd_len,
                       bool trusted_local,
                       char *result, size_t result_len)
{
    char line[COMMAND_MAX];
    char *argv[MAX_ARGS];
    int argc;
    char virt[PATH_MAX_LOCAL], actual[PATH_MAX_LOCAL + 8];

    if (!result || result_len == 0 || !cwd || cwd_len < 2) {
        return false;
    }
    result[0] = '\0';
    if (!ensure_ready(result, result_len)) {
        return false;
    }
    if (!command || command[0] == '\0' || strlen(command) >= sizeof(line)) {
        snprintf(result, result_len, "Usage: shell_exec(command)");
        return false;
    }
    if (cwd[0] != '/') {
        snprintf(cwd, cwd_len, "%s", ESP_SHELL_HOME);
    }
    strcpy(line, command);
    argc = split_args(line, argv, MAX_ARGS);
    if (argc == 0) {
        result[0] = '\0';
        return true;
    }

    if (!trusted_local && argc > 1 && strcmp(argv[0], "claw") == 0 &&
        (strcmp(argv[1], "install") == 0 || strcmp(argv[1], "use") == 0 ||
         strcmp(argv[1], "remove") == 0)) {
        snprintf(result, result_len, "Error: agent-pack changes require the local serial terminal");
        return false;
    }
    if (!trusted_local &&
        (strcmp(argv[0], "write") == 0 || strcmp(argv[0], "append") == 0 ||
         strcmp(argv[0], "touch") == 0 || strcmp(argv[0], "mkdir") == 0 ||
         strcmp(argv[0], "rm") == 0 || strcmp(argv[0], "rmdir") == 0 ||
         strcmp(argv[0], "cp") == 0 || strcmp(argv[0], "mv") == 0)) {
        for (int i = 1; i < argc; i++) {
            if (strstr(argv[i], ".claw")) {
                snprintf(result, result_len, "Error: .claw is reserved for local package management");
                return false;
            }
        }
    }

    if (strcmp(argv[0], "help") == 0) {
        snprintf(result, result_len,
                 "ESP shell commands:\n"
                 "  help uname hostname whoami id pwd cd ls cat echo\n"
                 "  write append touch mkdir rmdir rm cp mv\n"
                 "  df free uptime date ifconfig ip clear claw\n"
                 "Prefix local commands with ! (example: !ls). Normal text chats with zclaw.");
        return true;
    }
    if (strcmp(argv[0], "pwd") == 0) {
        snprintf(result, result_len, "%s", cwd);
        return true;
    }
    if (strcmp(argv[0], "cd") == 0) {
        const char *target = argc > 1 ? argv[1] : ESP_SHELL_HOME;
        struct stat st;
        if (!resolve_path(cwd, target, virt, sizeof(virt), actual, sizeof(actual)) ||
            stat(actual, &st) != 0 || !S_ISDIR(st.st_mode)) {
            snprintf(result, result_len, "cd: %s: directory not found", target);
            return false;
        }
        snprintf(cwd, cwd_len, "%s", virt);
        snprintf(result, result_len, "%s", cwd);
        return true;
    }
    if (strcmp(argv[0], "ls") == 0) {
        const char *path = argc > 1 && argv[1][0] != '-' ? argv[1] : cwd;
        return list_at(cwd, path, result, result_len);
    }
    if (strcmp(argv[0], "cat") == 0) {
        if (argc < 2) { snprintf(result, result_len, "cat: missing file"); return false; }
        return read_at(cwd, argv[1], result, result_len);
    }
    if (strcmp(argv[0], "echo") == 0) {
        char *cursor = result;
        size_t remaining = result_len;
        for (int i = 1; i < argc; i++) {
            if (i > 1) append_text(&cursor, &remaining, " ");
            append_text(&cursor, &remaining, argv[i]);
        }
        return true;
    }
    if (strcmp(argv[0], "write") == 0 || strcmp(argv[0], "append") == 0) {
        if (argc < 3) { snprintf(result, result_len, "%s: usage: %s FILE TEXT", argv[0], argv[0]); return false; }
        char content[COMMAND_MAX];
        char *cursor = content;
        size_t remaining = sizeof(content);
        content[0] = '\0';
        for (int i = 2; i < argc; i++) {
            if (i > 2) append_text(&cursor, &remaining, " ");
            append_text(&cursor, &remaining, argv[i]);
        }
        return write_at(cwd, argv[1], content, strcmp(argv[0], "append") == 0,
                        result, result_len);
    }
    if (strcmp(argv[0], "touch") == 0) {
        if (argc < 2) { snprintf(result, result_len, "touch: missing file"); return false; }
        return write_at(cwd, argv[1], "", true, result, result_len);
    }
    if (strcmp(argv[0], "mkdir") == 0) {
        if (argc < 2 || !resolve_path(cwd, argv[1], virt, sizeof(virt), actual, sizeof(actual))) {
            snprintf(result, result_len, "mkdir: invalid path"); return false;
        }
        if (mkdir(actual, 0755) != 0 && errno != EEXIST) {
            snprintf(result, result_len, "mkdir: %s: %s", virt, strerror(errno)); return false;
        }
        snprintf(result, result_len, "Created %s", virt);
        return true;
    }
    if (strcmp(argv[0], "rm") == 0 || strcmp(argv[0], "rmdir") == 0) {
        if (argc < 2) { snprintf(result, result_len, "%s: missing path", argv[0]); return false; }
        return remove_at(cwd, argv[1], result, result_len);
    }
    if (strcmp(argv[0], "cp") == 0) {
        if (argc < 3) { snprintf(result, result_len, "cp: usage: cp SOURCE DEST"); return false; }
        return copy_file_at(cwd, argv[1], argv[2], result, result_len);
    }
    if (strcmp(argv[0], "mv") == 0) {
        char dv[PATH_MAX_LOCAL], da[PATH_MAX_LOCAL + 8];
        if (argc < 3 || !resolve_path(cwd, argv[1], virt, sizeof(virt), actual, sizeof(actual)) ||
            !resolve_path(cwd, argv[2], dv, sizeof(dv), da, sizeof(da))) {
            snprintf(result, result_len, "mv: invalid path"); return false;
        }
        if (rename(actual, da) != 0) {
            snprintf(result, result_len, "mv: %s", strerror(errno)); return false;
        }
        snprintf(result, result_len, "Moved %s to %s", virt, dv);
        return true;
    }
    if (strcmp(argv[0], "uname") == 0) {
        esp_chip_info_t info;
        esp_chip_info(&info);
        snprintf(result, result_len, argc > 1 && strcmp(argv[1], "-a") == 0
                 ? "ESP-FreeRTOS esp32 zclaw-shell Xtensa cores=%d revision=%d"
                 : "ESP-FreeRTOS", info.cores, info.revision);
        return true;
    }
    if (strcmp(argv[0], "hostname") == 0) { snprintf(result, result_len, "esp32"); return true; }
    if (strcmp(argv[0], "whoami") == 0) { snprintf(result, result_len, "esp"); return true; }
    if (strcmp(argv[0], "id") == 0) { snprintf(result, result_len, "uid=1000(esp) gid=1000(esp)"); return true; }
    if (strcmp(argv[0], "free") == 0) {
        snprintf(result, result_len, "heap total=%u free=%u largest=%u bytes",
                 heap_caps_get_total_size(MALLOC_CAP_8BIT),
                 heap_caps_get_free_size(MALLOC_CAP_8BIT),
                 heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
        return true;
    }
    if (strcmp(argv[0], "uptime") == 0) {
        unsigned long seconds = (unsigned long)(esp_timer_get_time() / 1000000ULL);
        snprintf(result, result_len, "up %lud %02lu:%02lu:%02lu", seconds / 86400,
                 (seconds / 3600) % 24, (seconds / 60) % 60, seconds % 60);
        return true;
    }
    if (strcmp(argv[0], "date") == 0) {
        time_t now = time(NULL);
        struct tm tm_now;
        localtime_r(&now, &tm_now);
        strftime(result, result_len, "%a %b %d %H:%M:%S %Z %Y", &tm_now);
        return true;
    }
    if (strcmp(argv[0], "ifconfig") == 0 || strcmp(argv[0], "ip") == 0) {
        esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        esp_netif_ip_info_t ip;
        if (netif && esp_netif_get_ip_info(netif, &ip) == ESP_OK) {
            snprintf(result, result_len, "wlan0: inet " IPSTR " netmask " IPSTR " gateway " IPSTR,
                     IP2STR(&ip.ip), IP2STR(&ip.netmask), IP2STR(&ip.gw));
            return true;
        }
        snprintf(result, result_len, "wlan0: not connected");
        return true;
    }
    if (strcmp(argv[0], "df") == 0) {
        uint64_t total = 0, free_bytes = 0;
        esp_err_t err = esp_vfs_fat_info(FS_ROOT, &total, &free_bytes);
        if (err != ESP_OK) { snprintf(result, result_len, "df: unavailable"); return false; }
        snprintf(result, result_len, "Filesystem  Size  Used  Avail\n/fs        %uK  %uK  %uK",
                 (unsigned)(total / 1024), (unsigned)((total - free_bytes) / 1024),
                 (unsigned)(free_bytes / 1024));
        return true;
    }
    if (strcmp(argv[0], "clear") == 0) { snprintf(result, result_len, "\033[2J\033[H"); return true; }
    if (strcmp(argv[0], "claw") == 0) {
        if (argc < 2 || strcmp(argv[1], "help") == 0) {
            snprintf(result, result_len,
                     "claw agent-pack manager:\n"
                     "  claw install HTTPS_URL\n"
                     "  claw list\n"
                     "  claw use NAME\n"
                     "  claw status\n"
                     "  claw remove NAME\n"
                     "Packs are declarative ESP agents, not Linux binaries or shell scripts.");
            return true;
        }
        if (strcmp(argv[1], "install") == 0 && argc >= 3)
            return agent_pack_install(argv[2], result, result_len);
        if (strcmp(argv[1], "list") == 0)
            return agent_pack_list(result, result_len);
        if (strcmp(argv[1], "use") == 0 && argc >= 3)
            return agent_pack_use(argv[2], result, result_len);
        if (strcmp(argv[1], "status") == 0)
            return agent_pack_status(result, result_len);
        if (strcmp(argv[1], "remove") == 0 && argc >= 3)
            return agent_pack_remove(argv[2], result, result_len);
        snprintf(result, result_len, "claw: invalid command (try claw help)");
        return false;
    }

    snprintf(result, result_len, "%s: command not found (try help)", argv[0]);
    return false;
}
