
# The following lines of boilerplate have to be in your project's CMakeLists
# in this exact order for cmake to work correctlycmake_minimum_required(VERSION 3.5)
cmake_minimum_required(VERSION 3.5)
add_compile_options(-fdiagnostics-color=always)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

message("EXTRA_COMPONENT_DIRS: " ${EXTRA_COMPONENT_DIRS})

# 2.4GHz channel (non-overlapping: 1, 6, 11): idf.py -DCHANNEL=11 build
if(DEFINED CHANNEL)
    add_compile_definitions(CHANNEL=${CHANNEL})
endif()

string(REGEX REPLACE ".*/\(.*\)" "\\1" CURDIR ${CMAKE_CURRENT_SOURCE_DIR})
project(${CURDIR})

git_describe(PROJECT_VERSION ${COMPONENT_DIR})
message("Project commit: " ${PROJECT_VERSION})
