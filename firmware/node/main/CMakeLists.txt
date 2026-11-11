# The node's portable C core (firmware/core, tested on the PC by firmware/core/test.sh) plus the ESP-IDF glue.
set(core "${CMAKE_CURRENT_LIST_DIR}/../../core")
idf_component_register(
    SRCS "node_main.c" "node_auth.c"
         "${core}/motion_score.c" "${core}/round_broadcast.c" "${core}/gateway_record.c" "${core}/espnow_frame.c"
         "${core}/csi_layout.c" "${core}/link_table.c" "${core}/round_scheduler.c" "${core}/node_settings.c"
         "${core}/round_auth.c"
    INCLUDE_DIRS "." "${core}"
    REQUIRES esp_wifi esp_timer nvs_flash esp_driver_usb_serial_jtag mbedtls)
