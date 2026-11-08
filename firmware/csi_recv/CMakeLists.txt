
# The following lines of boilerplate have to be in your project's CMakeLists
# in this exact order for cmake to work correctlycmake_minimum_required(VERSION 3.5)
cmake_minimum_required(VERSION 3.5)
add_compile_options(-fdiagnostics-color=always)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

message("EXTRA_COMPONENT_DIRS: " ${EXTRA_COMPONENT_DIRS})

# Wireless receivers (see main/app_main.c): idf.py -DCSI_FORWARD=1 build, or -DCSI_GATEWAY=1 for the Pi's board.
foreach(flag CSI_FORWARD CSI_GATEWAY)
    if(DEFINED ${flag})
        add_compile_definitions(${flag}=${${flag}})
    endif()
endforeach()

string(REGEX REPLACE ".*/\(.*\)" "\\1" CURDIR ${CMAKE_CURRENT_SOURCE_DIR})
project(${CURDIR})

git_describe(PROJECT_VERSION ${COMPONENT_DIR})
message("Project commit: " ${PROJECT_VERSION})
