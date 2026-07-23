#include "json_util.h"
#include "config.h"
#include "tools.h"
#include "user_tools.h"
#include "llm.h"
#include "cJSON.h"
#include "esp_log.h"
#include <string.h>
#include <stdlib.h>
#include <stdio.h>

static const char *TAG = "json";

// Keep parsed response tree alive for tool_input access
static cJSON *s_parsed_response = NULL;

/*
 * cJSON_PrintUnformatted grows its heap buffer geometrically.  On the
 * classic ESP32, the full tool schema can fit in RAM while that final growth
 * step still fails because the heap has become fragmented.  Allocate the
 * known request ceiling once, before printing, so normal agent requests have
 * a predictable single allocation.
 */
static char *print_request_json(cJSON *root, size_t buffer_size)
{
    buffer_size += 5; // cJSON safety margin
    char *json_str = malloc(buffer_size);
    if (!json_str) {
        ESP_LOGE(TAG, "Failed to allocate %d-byte request buffer", (int)buffer_size);
        return NULL;
    }

    if (!cJSON_PrintPreallocated(root, json_str, (int)buffer_size, false)) {
        ESP_LOGE(TAG, "LLM request exceeds %d bytes", (int)(buffer_size - 5));
        free(json_str);
        return NULL;
    }

    return json_str;
}

static bool add_token_limit_field(cJSON *root)
{
    const char *field = "max_tokens";
    if (llm_get_backend() == LLM_BACKEND_OPENAI) {
        // GPT-5 chat-completions models reject max_tokens and require max_completion_tokens.
        field = "max_completion_tokens";
    }
    return cJSON_AddNumberToObject(root, field, LLM_MAX_TOKENS) != NULL;
}

static bool history_has_prior_tool_use(
    const conversation_msg_t *history,
    int index,
    const char *tool_id)
{
    if (!tool_id || tool_id[0] == '\0') {
        return false;
    }
    for (int i = 0; i < index; i++) {
        if (history[i].is_tool_use && strcmp(history[i].tool_id, tool_id) == 0) {
            return true;
        }
    }
    return false;
}

// -----------------------------------------------------------------------------
// Anthropic Format (Claude API)
// -----------------------------------------------------------------------------

static char *build_anthropic_request(
    const char *system_prompt,
    const conversation_msg_t *history,
    int history_len,
    const char *user_message,
    const tool_def_t *tools,
    int tool_count,
    size_t request_buffer_size)
{
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return NULL;
    }

    if (!cJSON_AddStringToObject(root, "model", llm_get_model()) ||
        !cJSON_AddNumberToObject(root, "max_tokens", LLM_MAX_TOKENS) ||
        !cJSON_AddStringToObject(root, "system", system_prompt)) {
        goto fail;
    }

    cJSON *messages = cJSON_AddArrayToObject(root, "messages");
    if (!messages) {
        goto fail;
    }

    // Add history
    for (int i = 0; i < history_len; i++) {
        cJSON *msg = cJSON_CreateObject();
        if (!msg || !cJSON_AddStringToObject(msg, "role", history[i].role)) {
            cJSON_Delete(msg);
            goto fail;
        }

        if (history[i].is_tool_use) {
            cJSON *content = cJSON_AddArrayToObject(msg, "content");
            cJSON *tool_use = cJSON_CreateObject();
            if (!content || !tool_use ||
                !cJSON_AddStringToObject(tool_use, "type", "tool_use") ||
                !cJSON_AddStringToObject(tool_use, "id", history[i].tool_id) ||
                !cJSON_AddStringToObject(tool_use, "name", history[i].tool_name)) {
                cJSON_Delete(tool_use);
                cJSON_Delete(msg);
                goto fail;
            }

            cJSON *input = cJSON_Parse(history[i].content);
            if (!input) {
                input = cJSON_CreateObject();
            }
            if (!input) {
                cJSON_Delete(tool_use);
                cJSON_Delete(msg);
                goto fail;
            }

            cJSON_AddItemToObject(tool_use, "input", input);
            cJSON_AddItemToArray(content, tool_use);
        } else if (history[i].is_tool_result) {
            if (!history_has_prior_tool_use(history, i, history[i].tool_id)) {
                ESP_LOGW(TAG, "Skipping orphan tool_result in history[%d] (id=%s)",
                         i, history[i].tool_id);
                cJSON_Delete(msg);
                continue;
            }
            cJSON *content = cJSON_AddArrayToObject(msg, "content");
            cJSON *tool_result = cJSON_CreateObject();
            if (!content || !tool_result ||
                !cJSON_AddStringToObject(tool_result, "type", "tool_result") ||
                !cJSON_AddStringToObject(tool_result, "tool_use_id", history[i].tool_id) ||
                !cJSON_AddStringToObject(tool_result, "content", history[i].content)) {
                cJSON_Delete(tool_result);
                cJSON_Delete(msg);
                goto fail;
            }

            cJSON_AddItemToArray(content, tool_result);
        } else if (!cJSON_AddStringToObject(msg, "content", history[i].content)) {
            cJSON_Delete(msg);
            goto fail;
        }

        cJSON_AddItemToArray(messages, msg);
    }

    // Add new user message
    if (user_message && user_message[0] != '\0') {
        cJSON *user_msg = cJSON_CreateObject();
        if (!user_msg ||
            !cJSON_AddStringToObject(user_msg, "role", "user") ||
            !cJSON_AddStringToObject(user_msg, "content", user_message)) {
            cJSON_Delete(user_msg);
            goto fail;
        }

        cJSON_AddItemToArray(messages, user_msg);
    }

    // Tools array (built-in + user-defined)
    int user_tool_count = user_tools_count();
    if (tool_count > 0 || user_tool_count > 0) {
        cJSON *tools_arr = cJSON_AddArrayToObject(root, "tools");
        if (!tools_arr) {
            goto fail;
        }

        // Built-in tools
        for (int i = 0; i < tool_count; i++) {
            cJSON *tool = cJSON_CreateObject();
            if (!tool ||
                !cJSON_AddStringToObject(tool, "name", tools[i].name) ||
                !cJSON_AddStringToObject(tool, "description", tools[i].description)) {
                cJSON_Delete(tool);
                goto fail;
            }

            cJSON *schema = cJSON_Parse(tools[i].input_schema_json);
            if (!schema) {
                schema = cJSON_CreateObject();
            }
            if (!schema) {
                cJSON_Delete(tool);
                goto fail;
            }
            cJSON_AddItemToObject(tool, "input_schema", schema);

            cJSON_AddItemToArray(tools_arr, tool);
        }

        // User-defined tools
        user_tool_t user_tools_arr[MAX_DYNAMIC_TOOLS];
        int loaded = user_tools_get_all(user_tools_arr, MAX_DYNAMIC_TOOLS);
        for (int i = 0; i < loaded; i++) {
            cJSON *tool = cJSON_CreateObject();
            cJSON *schema = cJSON_CreateObject();
            cJSON *properties = cJSON_CreateObject();
            if (!tool || !schema || !properties ||
                !cJSON_AddStringToObject(tool, "name", user_tools_arr[i].name) ||
                !cJSON_AddStringToObject(tool, "description", user_tools_arr[i].description) ||
                !cJSON_AddStringToObject(schema, "type", "object")) {
                cJSON_Delete(properties);
                cJSON_Delete(schema);
                cJSON_Delete(tool);
                goto fail;
            }

            cJSON_AddItemToObject(schema, "properties", properties);
            cJSON_AddItemToObject(tool, "input_schema", schema);
            cJSON_AddItemToArray(tools_arr, tool);
        }
    }

    char *json_str = print_request_json(root, request_buffer_size);
    if (!json_str) {
        goto fail;
    }

    cJSON_Delete(root);
    return json_str;

fail:
    cJSON_Delete(root);
    return NULL;
}

static bool parse_anthropic_response(
    cJSON *root,
    char *text_out,
    size_t text_out_len,
    char *tool_name_out,
    size_t tool_name_len,
    char *tool_id_out,
    size_t tool_id_len,
    cJSON **tool_input_out)
{
    cJSON *content = cJSON_GetObjectItem(root, "content");
    if (!content || !cJSON_IsArray(content)) {
        ESP_LOGE(TAG, "No content array in response");
        return false;
    }

    cJSON *block;
    cJSON_ArrayForEach(block, content) {
        cJSON *type = cJSON_GetObjectItem(block, "type");
        if (!type || !cJSON_IsString(type)) continue;

        if (strcmp(type->valuestring, "text") == 0) {
            cJSON *text = cJSON_GetObjectItem(block, "text");
            if (text && cJSON_IsString(text)) {
                strncpy(text_out, text->valuestring, text_out_len - 1);
                text_out[text_out_len - 1] = '\0';
            }
        } else if (strcmp(type->valuestring, "tool_use") == 0) {
            cJSON *name = cJSON_GetObjectItem(block, "name");
            cJSON *id = cJSON_GetObjectItem(block, "id");
            cJSON *input = cJSON_GetObjectItem(block, "input");

            if (name && cJSON_IsString(name)) {
                strncpy(tool_name_out, name->valuestring, tool_name_len - 1);
                tool_name_out[tool_name_len - 1] = '\0';
            }
            if (id && cJSON_IsString(id)) {
                strncpy(tool_id_out, id->valuestring, tool_id_len - 1);
                tool_id_out[tool_id_len - 1] = '\0';
            }
            if (input) {
                *tool_input_out = input;
            }
        }
    }

    return true;
}

// -----------------------------------------------------------------------------
// OpenAI Format (OpenAI, OpenRouter, Gemini, Ollama)
// -----------------------------------------------------------------------------

static char *build_openai_request(
    const char *system_prompt,
    const conversation_msg_t *history,
    int history_len,
    const char *user_message,
    const tool_def_t *tools,
    int tool_count,
    size_t request_buffer_size)
{
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return NULL;
    }

    if (!cJSON_AddStringToObject(root, "model", llm_get_model()) ||
        !add_token_limit_field(root)) {
        goto fail;
    }

    cJSON *messages = cJSON_AddArrayToObject(root, "messages");
    if (!messages) {
        goto fail;
    }

    // System message first
    cJSON *sys_msg = cJSON_CreateObject();
    if (!sys_msg ||
        !cJSON_AddStringToObject(sys_msg, "role", "system") ||
        !cJSON_AddStringToObject(sys_msg, "content", system_prompt)) {
        cJSON_Delete(sys_msg);
        goto fail;
    }
    cJSON_AddItemToArray(messages, sys_msg);

    // Add history
    for (int i = 0; i < history_len; i++) {
        cJSON *msg = cJSON_CreateObject();
        if (!msg) {
            goto fail;
        }

        if (history[i].is_tool_use) {
            // Assistant message with tool_calls
            cJSON *tool_calls = NULL;
            cJSON *tc = NULL;
            cJSON *func = NULL;

            if (!cJSON_AddStringToObject(msg, "role", "assistant") ||
                !cJSON_AddNullToObject(msg, "content")) {
                cJSON_Delete(msg);
                goto fail;
            }

            tool_calls = cJSON_AddArrayToObject(msg, "tool_calls");
            tc = cJSON_CreateObject();
            func = cJSON_CreateObject();
            if (!tool_calls || !tc || !func ||
                !cJSON_AddStringToObject(tc, "id", history[i].tool_id) ||
                !cJSON_AddStringToObject(tc, "type", "function") ||
                !cJSON_AddStringToObject(func, "name", history[i].tool_name) ||
                !cJSON_AddStringToObject(func, "arguments", history[i].content)) {
                cJSON_Delete(func);
                cJSON_Delete(tc);
                cJSON_Delete(msg);
                goto fail;
            }

            cJSON_AddItemToObject(tc, "function", func);
            cJSON_AddItemToArray(tool_calls, tc);
        } else if (history[i].is_tool_result) {
            if (!history_has_prior_tool_use(history, i, history[i].tool_id)) {
                ESP_LOGW(TAG, "Skipping orphan tool_result in history[%d] (id=%s)",
                         i, history[i].tool_id);
                cJSON_Delete(msg);
                continue;
            }
            // Tool response message
            if (!cJSON_AddStringToObject(msg, "role", "tool") ||
                !cJSON_AddStringToObject(msg, "tool_call_id", history[i].tool_id) ||
                !cJSON_AddStringToObject(msg, "content", history[i].content)) {
                cJSON_Delete(msg);
                goto fail;
            }
        } else {
            // Regular message
            if (!cJSON_AddStringToObject(msg, "role", history[i].role) ||
                !cJSON_AddStringToObject(msg, "content", history[i].content)) {
                cJSON_Delete(msg);
                goto fail;
            }
        }

        cJSON_AddItemToArray(messages, msg);
    }

    // Add new user message
    if (user_message && user_message[0] != '\0') {
        cJSON *user_msg = cJSON_CreateObject();
        if (!user_msg ||
            !cJSON_AddStringToObject(user_msg, "role", "user") ||
            !cJSON_AddStringToObject(user_msg, "content", user_message)) {
            cJSON_Delete(user_msg);
            goto fail;
        }
        cJSON_AddItemToArray(messages, user_msg);
    }

    // Tools array (OpenAI format: built-in + user-defined)
    int user_tool_count = user_tools_count();
    if (tool_count > 0 || user_tool_count > 0) {
        cJSON *tools_arr = cJSON_AddArrayToObject(root, "tools");
        if (!tools_arr) {
            goto fail;
        }

        // Built-in tools
        for (int i = 0; i < tool_count; i++) {
            cJSON *tool = cJSON_CreateObject();
            cJSON *func = cJSON_CreateObject();
            cJSON *params = cJSON_Parse(tools[i].input_schema_json);
            if (!params) {
                params = cJSON_CreateObject();
            }

            if (!tool || !func || !params ||
                !cJSON_AddStringToObject(tool, "type", "function") ||
                !cJSON_AddStringToObject(func, "name", tools[i].name) ||
                !cJSON_AddStringToObject(func, "description", tools[i].description)) {
                cJSON_Delete(params);
                cJSON_Delete(func);
                cJSON_Delete(tool);
                goto fail;
            }

            cJSON_AddItemToObject(func, "parameters", params);
            cJSON_AddItemToObject(tool, "function", func);
            cJSON_AddItemToArray(tools_arr, tool);
        }

        // User-defined tools
        user_tool_t user_tools_arr[MAX_DYNAMIC_TOOLS];
        int loaded = user_tools_get_all(user_tools_arr, MAX_DYNAMIC_TOOLS);
        for (int i = 0; i < loaded; i++) {
            cJSON *tool = cJSON_CreateObject();
            cJSON *func = cJSON_CreateObject();
            cJSON *params = cJSON_CreateObject();
            cJSON *properties = cJSON_CreateObject();
            if (!tool || !func || !params || !properties ||
                !cJSON_AddStringToObject(tool, "type", "function") ||
                !cJSON_AddStringToObject(func, "name", user_tools_arr[i].name) ||
                !cJSON_AddStringToObject(func, "description", user_tools_arr[i].description) ||
                !cJSON_AddStringToObject(params, "type", "object")) {
                cJSON_Delete(properties);
                cJSON_Delete(params);
                cJSON_Delete(func);
                cJSON_Delete(tool);
                goto fail;
            }

            cJSON_AddItemToObject(params, "properties", properties);
            cJSON_AddItemToObject(func, "parameters", params);
            cJSON_AddItemToObject(tool, "function", func);
            cJSON_AddItemToArray(tools_arr, tool);
        }
    }

    char *json_str = print_request_json(root, request_buffer_size);
    if (!json_str) {
        goto fail;
    }

    cJSON_Delete(root);
    return json_str;

fail:
    cJSON_Delete(root);
    return NULL;
}

static bool parse_openai_response(
    cJSON *root,
    char *text_out,
    size_t text_out_len,
    char *tool_name_out,
    size_t tool_name_len,
    char *tool_id_out,
    size_t tool_id_len,
    cJSON **tool_input_out)
{
    // OpenAI: choices[0].message
    cJSON *choices = cJSON_GetObjectItem(root, "choices");
    if (!choices || !cJSON_IsArray(choices) || cJSON_GetArraySize(choices) == 0) {
        ESP_LOGE(TAG, "No choices in response");
        return false;
    }

    cJSON *choice = cJSON_GetArrayItem(choices, 0);
    cJSON *message = cJSON_GetObjectItem(choice, "message");
    if (!message) {
        ESP_LOGE(TAG, "No message in choice");
        return false;
    }

    // Check for text content
    cJSON *content = cJSON_GetObjectItem(message, "content");
    if (content && cJSON_IsString(content)) {
        strncpy(text_out, content->valuestring, text_out_len - 1);
        text_out[text_out_len - 1] = '\0';
    }

    // Check for tool_calls
    cJSON *tool_calls = cJSON_GetObjectItem(message, "tool_calls");
    if (tool_calls && cJSON_IsArray(tool_calls) && cJSON_GetArraySize(tool_calls) > 0) {
        cJSON *tc = cJSON_GetArrayItem(tool_calls, 0);

        cJSON *id = cJSON_GetObjectItem(tc, "id");
        if (id && cJSON_IsString(id)) {
            strncpy(tool_id_out, id->valuestring, tool_id_len - 1);
            tool_id_out[tool_id_len - 1] = '\0';
        }

        cJSON *func = cJSON_GetObjectItem(tc, "function");
        if (func) {
            cJSON *name = cJSON_GetObjectItem(func, "name");
            if (name && cJSON_IsString(name)) {
                strncpy(tool_name_out, name->valuestring, tool_name_len - 1);
                tool_name_out[tool_name_len - 1] = '\0';
            }

            // Parse arguments string into JSON
            cJSON *args = cJSON_GetObjectItem(func, "arguments");
            if (args && cJSON_IsString(args)) {
                cJSON *parsed_args = cJSON_Parse(args->valuestring);
                if (!parsed_args) {
                    parsed_args = cJSON_CreateObject();
                }
                if (parsed_args) {
                    cJSON_AddItemToObject(tc, "_parsed_arguments", parsed_args);
                    *tool_input_out = parsed_args;
                }
            }
        }
    }

    return true;
}

// -----------------------------------------------------------------------------
// Public API
// -----------------------------------------------------------------------------

char *json_build_request(
    const char *system_prompt,
    const conversation_msg_t *history,
    int history_len,
    const char *user_message,
    const tool_def_t *tools,
    int tool_count)
{
    char *json_str;
    // A classic ESP32 has limited contiguous heap after Wi-Fi, TLS, and the
    // Telegram task are running.  Sending every schema (33+) may leave no
    // room for cJSON to hold the request and its printable form at once.
    // Keep the useful local-control tools available in a compact fallback.
    static const char *const compact_tool_names[] = {
        "gpio_write", "gpio_read", "gpio_read_all", "delay", "i2c_scan",
        "shell_exec", "filesystem_list", "filesystem_read", "filesystem_write",
        "get_diagnostics"
    };
    tool_def_t compact_tools[sizeof(compact_tool_names) / sizeof(compact_tool_names[0])];
    int compact_count = 0;
    bool use_compact_tools = false;

#define BUILD_REQUEST(tool_list, tool_list_count, buffer_size) \
    (llm_is_openai_format() \
         ? build_openai_request(system_prompt, history, history_len, user_message, \
                                tool_list, tool_list_count, buffer_size) \
         : build_anthropic_request(system_prompt, history, history_len, user_message, \
                                   tool_list, tool_list_count, buffer_size))

    for (int wanted = 0;
         wanted < (int)(sizeof(compact_tool_names) / sizeof(compact_tool_names[0]));
         wanted++) {
        for (int i = 0; i < tool_count; i++) {
            if (strcmp(tools[i].name, compact_tool_names[wanted]) == 0) {
                compact_tools[compact_count++] = tools[i];
                break;
            }
        }
    }

    // The original ESP32 has no PSRAM.  Constructing every tool schema first
    // can momentarily leave less than 1 KB free and destabilize the following
    // TLS request even when a compact request would fit.  Start compact there.
#if defined(CONFIG_IDF_TARGET_ESP32)
    use_compact_tools = true;
#endif

    json_str = use_compact_tools
                   ? BUILD_REQUEST(compact_tools, compact_count, 8192)
                   : BUILD_REQUEST(tools, tool_count, LLM_REQUEST_BUF_SIZE);

    if (!json_str && !use_compact_tools && tools && tool_count > 0) {
        ESP_LOGW(TAG, "Full tool catalogue did not fit; retrying with %d compact tools",
                 compact_count);
        json_str = BUILD_REQUEST(compact_tools, compact_count, 8192);
    }

    if (!json_str) {
        ESP_LOGW(TAG, "Tool schemas did not fit; retrying chat-only request");
        json_str = BUILD_REQUEST(NULL, 0, 4096);
    }

#undef BUILD_REQUEST

    if (json_str) {
        ESP_LOGD(TAG, "Built request: %d bytes", (int)strlen(json_str));
    }

    return json_str;
}

bool json_parse_response(
    const char *response_json,
    char *text_out,
    size_t text_out_len,
    char *tool_name_out,
    size_t tool_name_len,
    char *tool_id_out,
    size_t tool_id_len,
    cJSON **tool_input_out)
{
    // Free any previous parsed response
    json_free_parsed_response();

    text_out[0] = '\0';
    tool_name_out[0] = '\0';
    tool_id_out[0] = '\0';
    *tool_input_out = NULL;

    s_parsed_response = cJSON_Parse(response_json);
    if (!s_parsed_response) {
        ESP_LOGE(TAG, "Failed to parse response JSON");
        return false;
    }

    // Check for error (both APIs use similar format)
    cJSON *error = cJSON_GetObjectItem(s_parsed_response, "error");
    if (error) {
        cJSON *msg = cJSON_GetObjectItem(error, "message");
        if (msg && cJSON_IsString(msg)) {
            snprintf(text_out, text_out_len, "API Error: %s", msg->valuestring);
        } else {
            snprintf(text_out, text_out_len, "API Error (unknown)");
        }
        return true;
    }

    // Parse based on format
    if (llm_is_openai_format()) {
        return parse_openai_response(s_parsed_response, text_out, text_out_len,
                                      tool_name_out, tool_name_len,
                                      tool_id_out, tool_id_len, tool_input_out);
    } else {
        return parse_anthropic_response(s_parsed_response, text_out, text_out_len,
                                         tool_name_out, tool_name_len,
                                         tool_id_out, tool_id_len, tool_input_out);
    }
}

void json_free_parsed_response(void)
{
    if (s_parsed_response) {
        cJSON_Delete(s_parsed_response);
        s_parsed_response = NULL;
    }
}
