
# The following lines of boilerplate have to be in your project's CMakeLists
# in this exact order for cmake to work correctlycmake_minimum_required(VERSION 3.5)
cmake_minimum_required(VERSION 3.5)
add_compile_options(-fdiagnostics-color=always)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# This node's address byte (1a:00:00:00:00:<id>) and how many transceivers are in the
# rotation: idf.py -DSENDER_ID=0x01 -DNUM_SENDERS=2 build
if(DEFINED SENDER_ID)
    add_compile_definitions(SENDER_ID=${SENDER_ID})
endif()
if(DEFINED NUM_SENDERS)
    add_compile_definitions(NUM_SENDERS=${NUM_SENDERS})
endif()

string(REGEX REPLACE ".*/\(.*\)" "\\1" CURDIR ${CMAKE_CURRENT_SOURCE_DIR})
project(${CURDIR})

git_describe(PROJECT_VERSION ${COMPONENT_DIR})
message("Project commit: " ${PROJECT_VERSION})
