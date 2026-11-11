# The node image: one build for every board (firmware/README.md "Node image"). Role, channel and slot are runtime
# settings (scripts/node.py), never build flags.
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(node)
