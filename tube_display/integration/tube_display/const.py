"""Constants for the Tube Display integration."""

DOMAIN = "tube_display"
DEFAULT_PORT = 8098

CONF_CLOCK = "clock"

SERVICE_SHOW_SCREEN = "show_screen"
SERVICE_FLASH_MESSAGE = "flash_message"
SERVICE_PAUSE = "pause"
SERVICE_RESUME = "resume"
SERVICE_NEXT_SCREEN = "next_screen"
SERVICE_SET_ROTATION = "set_rotation"

# notice codes the app reports in GET /api/status -> clock_info.notices (features the
# screens use that the clock's firmware cannot do); each becomes a Repairs issue
NOTICE_GIF = "gif_needs_2_4"
NOTICE_TRANSITIONS = "transitions_need_2_5"
NOTICE_FLIP = "flip_needs_2_6"
NOTICE_CODES = (NOTICE_GIF, NOTICE_TRANSITIONS, NOTICE_FLIP)
