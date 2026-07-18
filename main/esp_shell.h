#ifndef ESP_SHELL_H
#define ESP_SHELL_H

#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>

#define ESP_SHELL_HOME "/home/esp"
#define ESP_SHELL_CWD_MAX 192

esp_err_t esp_shell_init(void);

bool esp_shell_execute(const char *command,
                       char *cwd,
                       size_t cwd_len,
                       bool trusted_local,
                       char *result,
                       size_t result_len);

bool esp_shell_list(const char *path, char *result, size_t result_len);
bool esp_shell_read(const char *path, char *result, size_t result_len);
bool esp_shell_write(const char *path,
                     const char *content,
                     bool append,
                     char *result,
                     size_t result_len);
bool esp_shell_remove(const char *path, char *result, size_t result_len);

#endif
