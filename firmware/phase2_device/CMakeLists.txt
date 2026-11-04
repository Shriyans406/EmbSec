cmake_minimum_required(VERSION 3.13)

include(pico_sdk_import.cmake)

project(aeisp_phase2_device C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

add_executable(aeisp_phase2_device
    src/main.c
    src/device.c
    src/proto.c
)

target_include_directories(aeisp_phase2_device PRIVATE src)
target_compile_options(aeisp_phase2_device PRIVATE -Wall -Wextra)

target_link_libraries(aeisp_phase2_device
    pico_stdlib
    pico_unique_id
    pico_bootrom
    hardware_adc
    hardware_watchdog
    hardware_clocks
)

pico_enable_stdio_usb(aeisp_phase2_device 1)
pico_enable_stdio_uart(aeisp_phase2_device 0)

pico_add_extra_outputs(aeisp_phase2_device)