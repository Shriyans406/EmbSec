cmake_minimum_required(VERSION 3.13)

# AEISP harness firmware. This file is OURS; generated code only ever supplies .c/.h files through
# AEISP_APP_DIR. The build engine passes:
#   -DAEISP_APP_DIR=<folder with the app's .c/.h files>   (optional)
#   -DAEISP_BUILD_ID=<number>   -DAEISP_SRC_HASH=<short hash of the app sources>

set(AEISP_APP_DIR "" CACHE PATH "Folder containing the generated application sources")
set(AEISP_BUILD_ID "0" CACHE STRING "Build record number embedded in the firmware")
set(AEISP_SRC_HASH "none" CACHE STRING "Short hash of the application sources")

include(pico_sdk_import.cmake)

project(aeisp_firmware C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

pico_sdk_init()

set(APP_SOURCES "")
if(AEISP_APP_DIR)
    file(GLOB APP_SOURCES "${AEISP_APP_DIR}/*.c")
endif()

add_executable(aeisp_firmware
    src/main.c
    src/device.c
    src/proto.c
    src/app_default.c
    ${APP_SOURCES}
)

target_include_directories(aeisp_firmware PRIVATE src)
if(AEISP_APP_DIR)
    target_include_directories(aeisp_firmware PRIVATE "${AEISP_APP_DIR}")
endif()

target_compile_options(aeisp_firmware PRIVATE -Wall -Wextra)
target_compile_definitions(aeisp_firmware PRIVATE
    AEISP_BUILD_ID="${AEISP_BUILD_ID}"
    AEISP_SRC_HASH="${AEISP_SRC_HASH}"
)

target_link_libraries(aeisp_firmware
    pico_stdlib
    pico_unique_id
    pico_bootrom
    hardware_adc
    hardware_watchdog
    hardware_clocks
)

pico_enable_stdio_usb(aeisp_firmware 1)
pico_enable_stdio_uart(aeisp_firmware 0)

pico_add_extra_outputs(aeisp_firmware)