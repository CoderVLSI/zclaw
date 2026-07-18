#ifndef AGENT_PACK_H
#define AGENT_PACK_H

#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>

esp_err_t agent_pack_init(void);
const char *agent_pack_active_name(void);
const char *agent_pack_instructions(void);

bool agent_pack_install(const char *url, char *result, size_t result_len);
bool agent_pack_use(const char *name, char *result, size_t result_len);
bool agent_pack_list(char *result, size_t result_len);
bool agent_pack_status(char *result, size_t result_len);
bool agent_pack_remove(const char *name, char *result, size_t result_len);

#endif
