cmake_minimum_required(VERSION 3.16)

# Bare-metal cross-compilers cannot link CMake's default host-style probe.
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

set(ZCLAW_VERSION_FILE "${CMAKE_SOURCE_DIR}/VERSION")
set(PROJECT_VER "dev")
if(EXISTS "${ZCLAW_VERSION_FILE}")
    file(STRINGS "${ZCLAW_VERSION_FILE}" PROJECT_VER LIMIT_COUNT 1)
    string(STRIP "${PROJECT_VER}" PROJECT_VER)
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(zclaw)
