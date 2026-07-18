#include "agent_pack.h"
#include "esp_shell.h"

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PACK_FORMAT "zclaw-agent-pack-v1"
#define PACK_DIR "/home/esp/.claw/packs"
#define ACTIVE_FILE "/home/esp/.claw/active"
#define PACK_MAX_BYTES 4096
#define PACK_NAME_MAX 31
#define PACK_INSTRUCTIONS_MAX 639

static const char *TAG = "agent_pack";
static char s_active_name[PACK_NAME_MAX + 1];
static char s_instructions[PACK_INSTRUCTIONS_MAX + 1];

static bool valid_name(const char *name)
{
    size_t len;
    if (!name || (len = strlen(name)) == 0 || len > PACK_NAME_MAX) {
        return false;
    }
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)name[i];
        if (!(isalnum(c) || c == '-' || c == '_')) {
            return false;
        }
    }
    return true;
}

static void ensure_pack_dirs(void)
{
    char cwd[ESP_SHELL_CWD_MAX] = ESP_SHELL_HOME;
    char ignored[96];
    esp_shell_execute("mkdir /home/esp/.claw", cwd, sizeof(cwd), true, ignored, sizeof(ignored));
    esp_shell_execute("mkdir /home/esp/.claw/packs", cwd, sizeof(cwd), true, ignored, sizeof(ignored));
}

static bool pack_path(const char *name, char *path, size_t path_len)
{
    if (!valid_name(name)) {
        return false;
    }
    return snprintf(path, path_len, PACK_DIR "/%s.json", name) < (int)path_len;
}

static bool parse_pack(const char *json,
                       char *name,
                       size_t name_len,
                       char *instructions,
                       size_t instructions_len,
                       char *error,
                       size_t error_len)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) {
        snprintf(error, error_len, "Invalid JSON agent pack");
        return false;
    }
    const cJSON *format = cJSON_GetObjectItemCaseSensitive(root, "format");
    const cJSON *name_item = cJSON_GetObjectItemCaseSensitive(root, "name");
    const cJSON *instructions_item = cJSON_GetObjectItemCaseSensitive(root, "instructions");
    bool ok = cJSON_IsString(format) && strcmp(format->valuestring, PACK_FORMAT) == 0 &&
              cJSON_IsString(name_item) && valid_name(name_item->valuestring) &&
              cJSON_IsString(instructions_item) && instructions_item->valuestring[0] != '\0' &&
              strlen(instructions_item->valuestring) <= PACK_INSTRUCTIONS_MAX;
    if (!ok) {
        snprintf(error, error_len,
                 "Pack must contain format=%s, a safe name, and instructions <=%d bytes",
                 PACK_FORMAT, PACK_INSTRUCTIONS_MAX);
        cJSON_Delete(root);
        return false;
    }
    snprintf(name, name_len, "%s", name_item->valuestring);
    snprintf(instructions, instructions_len, "%s", instructions_item->valuestring);
    cJSON_Delete(root);
    return true;
}

static bool load_named(const char *name, char *result, size_t result_len)
{
    char path[128];
    char json[PACK_MAX_BYTES + 1];
    char parsed_name[PACK_NAME_MAX + 1];
    char instructions[PACK_INSTRUCTIONS_MAX + 1];
    char error[160];
    if (!pack_path(name, path, sizeof(path))) {
        snprintf(result, result_len, "Error: invalid agent pack name");
        return false;
    }
    if (!esp_shell_read(path, json, sizeof(json))) {
        snprintf(result, result_len, "Error: agent pack '%s' is not installed", name);
        return false;
    }
    if (!parse_pack(json, parsed_name, sizeof(parsed_name), instructions,
                    sizeof(instructions), error, sizeof(error))) {
        snprintf(result, result_len, "Error: invalid stored pack: %s", error);
        return false;
    }
    if (strcmp(parsed_name, name) != 0) {
        snprintf(result, result_len, "Error: stored pack name does not match filename");
        return false;
    }
    snprintf(s_active_name, sizeof(s_active_name), "%s", parsed_name);
    snprintf(s_instructions, sizeof(s_instructions), "%s", instructions);
    return true;
}

static bool http_get_text(const char *url, char *body, size_t body_len,
                          char *error, size_t error_len)
{
    if (!url || strncmp(url, "https://", 8) != 0) {
        snprintf(error, error_len, "Only HTTPS agent-pack URLs are allowed");
        return false;
    }
    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 20000,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .buffer_size = 1024,
        .buffer_size_tx = 512,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        snprintf(error, error_len, "HTTP client initialization failed");
        return false;
    }
    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        snprintf(error, error_len, "HTTPS open failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return false;
    }
    esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    if (status < 200 || status >= 300) {
        snprintf(error, error_len, "HTTPS returned status %d", status);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return false;
    }
    size_t used = 0;
    while (used < body_len - 1) {
        int got = esp_http_client_read(client, body + used, body_len - 1 - used);
        if (got < 0) {
            snprintf(error, error_len, "HTTPS read failed");
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
            return false;
        }
        if (got == 0) break;
        used += (size_t)got;
    }
    body[used] = '\0';
    if (used == body_len - 1 && !esp_http_client_is_complete_data_received(client)) {
        snprintf(error, error_len, "Agent pack exceeds %u bytes", (unsigned)(body_len - 1));
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return false;
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return true;
}

esp_err_t agent_pack_init(void)
{
    char active[PACK_NAME_MAX + 2];
    char ignored[160];
    s_active_name[0] = '\0';
    s_instructions[0] = '\0';
    ensure_pack_dirs();
    if (!esp_shell_read(ACTIVE_FILE, active, sizeof(active))) {
        return ESP_OK;
    }
    active[strcspn(active, "\r\n")] = '\0';
    if (!valid_name(active) || !load_named(active, ignored, sizeof(ignored))) {
        ESP_LOGW(TAG, "Ignoring invalid active agent pack");
    }
    return ESP_OK;
}

const char *agent_pack_active_name(void)
{
    return s_active_name[0] ? s_active_name : NULL;
}

const char *agent_pack_instructions(void)
{
    return s_instructions[0] ? s_instructions : NULL;
}

bool agent_pack_install(const char *url, char *result, size_t result_len)
{
    char *json = malloc(PACK_MAX_BYTES + 1);
    char name[PACK_NAME_MAX + 1];
    char instructions[PACK_INSTRUCTIONS_MAX + 1];
    char error[192];
    char path[128];
    if (!json) {
        snprintf(result, result_len, "Error: insufficient memory for download");
        return false;
    }
    bool ok = http_get_text(url, json, PACK_MAX_BYTES + 1, error, sizeof(error));
    if (!ok || !parse_pack(json, name, sizeof(name), instructions, sizeof(instructions),
                           error, sizeof(error)) || !pack_path(name, path, sizeof(path))) {
        snprintf(result, result_len, "claw install: %s", error);
        free(json);
        return false;
    }
    ensure_pack_dirs();
    ok = esp_shell_write(path, json, false, error, sizeof(error)) &&
         esp_shell_write(ACTIVE_FILE, name, false, error, sizeof(error));
    free(json);
    if (!ok || !load_named(name, error, sizeof(error))) {
        snprintf(result, result_len, "claw install: %s", error);
        return false;
    }
    snprintf(result, result_len, "Installed and activated agent pack '%s'", name);
    return true;
}

bool agent_pack_use(const char *name, char *result, size_t result_len)
{
    char error[160];
    if (!load_named(name, error, sizeof(error)) ||
        !esp_shell_write(ACTIVE_FILE, name, false, error, sizeof(error))) {
        snprintf(result, result_len, "claw use: %s", error);
        return false;
    }
    snprintf(result, result_len, "Activated agent pack '%s'", name);
    return true;
}

bool agent_pack_list(char *result, size_t result_len)
{
    return esp_shell_list(PACK_DIR, result, result_len);
}

bool agent_pack_status(char *result, size_t result_len)
{
    if (!s_active_name[0]) {
        snprintf(result, result_len, "No agent pack active. Base zclaw is running.");
    } else {
        snprintf(result, result_len, "Active agent pack: %s", s_active_name);
    }
    return true;
}

bool agent_pack_remove(const char *name, char *result, size_t result_len)
{
    char path[128];
    char error[160];
    if (!pack_path(name, path, sizeof(path)) || !esp_shell_remove(path, error, sizeof(error))) {
        snprintf(result, result_len, "claw remove: %s", error);
        return false;
    }
    if (strcmp(name, s_active_name) == 0) {
        esp_shell_remove(ACTIVE_FILE, error, sizeof(error));
        s_active_name[0] = '\0';
        s_instructions[0] = '\0';
    }
    snprintf(result, result_len, "Removed agent pack '%s'", name);
    return true;
}
