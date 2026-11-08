
# The following lines of boilerplate have to be in your project's CMakeLists
# in this exact order for cmake to work correctlycmake_minimum_required(VERSION 3.5)
cmake_minimum_required(VERSION 3.5)
add_compile_options(-fdiagnostics-color=always)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

message("EXTRA_COMPONENT_DIRS: " ${EXTRA_COMPONENT_DIRS})

# Build-time options, forwarded to main/app_main.c; firmware/README.md lists their defaults. Example:
#   idf.py -B build_c5_ch11 -DCSI_CHANNEL=11 set-target esp32c5
foreach(flag CSI_CHANNEL CSI_PRINT_STRIDE CSI_PRINT_RAW CSI_FORCE_GAIN CSI_GAIN_LOCK_STRONGEST CSI_GAIN_LOCK_OFFSET)
    if(DEFINED ${flag})
        add_compile_definitions(${flag}=${${flag}})
    endif()
endforeach()

string(REGEX REPLACE ".*/\(.*\)" "\\1" CURDIR ${CMAKE_CURRENT_SOURCE_DIR})
project(${CURDIR})

git_describe(PROJECT_VERSION ${COMPONENT_DIR})
message("Project commit: " ${PROJECT_VERSION})
