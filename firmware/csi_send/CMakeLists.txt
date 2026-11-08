
# The following lines of boilerplate have to be in your project's CMakeLists
# in this exact order for cmake to work correctlycmake_minimum_required(VERSION 3.5)
cmake_minimum_required(VERSION 3.5)
add_compile_options(-fdiagnostics-color=always)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# Build-time options, forwarded to main/app_main.c; firmware/README.md lists their defaults. Example:
#   idf.py -B build_c5_id01 -DSENDER_ID=0x01 -DCSI_SEND_HZ=30 -DCSI_CHANNEL=11 set-target esp32c5
# SENDER_ID is the last byte of the sender address 1a:0c:12:00:00:<id>.
foreach(flag SENDER_ID CSI_SEND_HZ CSI_CHANNEL)
    if(DEFINED ${flag})
        add_compile_definitions(${flag}=${${flag}})
    endif()
endforeach()

string(REGEX REPLACE ".*/\(.*\)" "\\1" CURDIR ${CMAKE_CURRENT_SOURCE_DIR})
project(${CURDIR})

git_describe(PROJECT_VERSION ${COMPONENT_DIR})
message("Project commit: " ${PROJECT_VERSION})
