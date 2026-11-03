cmake_minimum_required(VERSION 3.13)

include(pico_sdk_import.cmake)

project(aeisp_phase1_echo C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

add_executable(aeisp_phase1_echo main.c)

target_link_libraries(aeisp_phase1_echo pico_stdlib pico_unique_id pico_bootrom)

pico_enable_stdio_usb(aeisp_phase1_echo 1)
pico_enable_stdio_uart(aeisp_phase1_echo 0)

pico_add_extra_outputs(aeisp_phase1_echo)