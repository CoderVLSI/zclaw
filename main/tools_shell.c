#include "tools_handlers.h"
#include "esp_shell.h"

#include "cJSON.h"
#include <stdio.h>
#include <string.h>

static const char *json_string(const cJSON *input, const char *name)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(input, name);
    return cJSON_IsString(item) ? item->valuestring : NULL;
}

bool tools_shell_exec_handler(const cJSON *input, char *result, size_t result_len)
{
    const char *command = json_string(input, "command");
    const char *requested_cwd = json_string(input, "cwd");
    char cwd[ESP_SHELL_CWD_MAX];
    if (!command || command[0] == '\0') {
        snprintf(result, result_len, "Error: command is required");
        return false;
    }
    snprintf(cwd, sizeof(cwd), "%s", requested_cwd ? requested_cwd : ESP_SHELL_HOME);
    return esp_shell_execute(command, cwd, sizeof(cwd), false, result, result_len);
}

bool tools_filesystem_list_handler(const cJSON *input, char *result, size_t result_len)
{
    const char *path = json_string(input, "path");
    return esp_shell_list(path ? path : ESP_SHELL_HOME, result, result_len);
}

bool tools_filesystem_read_handler(const cJSON *input, char *result, size_t result_len)
{
    const char *path = json_string(input, "path");
    if (!path) {
        snprintf(result, result_len, "Error: path is required");
        return false;
    }
    return esp_shell_read(path, result, result_len);
}

bool tools_filesystem_write_handler(const cJSON *input, char *result, size_t result_len)
{
    const char *path = json_string(input, "path");
    const char *content = json_string(input, "content");
    const cJSON *append_item = cJSON_GetObjectItemCaseSensitive(input, "append");
    bool append = cJSON_IsTrue(append_item);
    if (!path || !content) {
        snprintf(result, result_len, "Error: path and content are required");
        return false;
    }
    if (strstr(path, ".claw")) {
        snprintf(result, result_len, "Error: .claw is reserved for local package management");
        return false;
    }
    return esp_shell_write(path, content, append, result, result_len);
}

bool tools_filesystem_remove_handler(const cJSON *input, char *result, size_t result_len)
{
    const char *path = json_string(input, "path");
    if (!path) {
        snprintf(result, result_len, "Error: path is required");
        return false;
    }
    if (strstr(path, ".claw")) {
        snprintf(result, result_len, "Error: .claw is reserved for local package management");
        return false;
    }
    return esp_shell_remove(path, result, result_len);
}
