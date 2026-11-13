"""Browser end-to-end tests of the tube_display screen editor.

Each test runs at both viewport sizes (desktop 1280x900 and phone 390x844, via
the `viewport` fixture in conftest.py) against the real editor app served by
stub_server.py (stub HA + two fake clocks). Run with:

    cd tube_display/app && python -m pytest e2e -v

or against a remote Chromium:  E2E_CDP_URL=http://localhost:9222 python -m pytest e2e -v

Selectors use the element ids of editor_static/index.html; waits are on
conditions (wait_for_function / waiting locators), not fixed sleeps.
"""

from __future__ import annotations

import yaml

# ---------------------------------------------------------------- helpers


def open_editor(page, base_url):
    """Load the editor and wait until the screen list and all six tube
    previews (with a rendered picture) are there."""
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.querySelectorAll('#screenList .screen-item').length === 4")
    page.wait_for_function("document.querySelectorAll('.tube-img').length === 6")
    page.wait_for_function("state.previewUrl !== null")


def select_screen(page, name):
    page.locator(f"#screenList li:text-is('{name}')").click()
    page.wait_for_function(
        f"state.selectedScreen === '{name}' && state.previewUrl !== null")


def click_tube(page, index):
    page.locator(".tube-img").nth(index).click()
    page.wait_for_selector("#tubeEditor:not(.hidden)")


def edit_tube0_text(page, new_text):
    """Select tube 0 of the selected screen and type a new text into its text
    field; waits until the preview picture was re-rendered (new blob URL)."""
    click_tube(page, 0)
    old_preview = page.evaluate("state.previewUrl")
    field = page.locator("#tubeFields .value-source .tab-body textarea").first
    field.wait_for(state="visible")
    field.fill(new_text)
    page.wait_for_function(
        "(prev) => state.previewUrl !== null && state.previewUrl !== prev",
        arg=old_preview)


def save(page):
    page.click("#saveBtn")
    page.wait_for_function(
        "document.getElementById('saveBtn').disabled === true"
        " && document.getElementById('status').textContent.trim() === 'saved'")


def read_screens_file(screens_file):
    with open(screens_file, encoding="utf-8") as f:
        return f.read()


def load_screens_file(screens_file):
    return yaml.safe_load(read_screens_file(screens_file))


# ---------------------------------------------------------------- a. load / layout


def test_loads_six_tube_previews_no_overflow(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    assert page.locator(".tube-img").count() == 6
    assert page.locator(".tube-img.broken").count() == 0
    # the first tube's preview is a rendered picture (blob URL as background)
    bg = page.evaluate(
        "getComputedStyle(document.querySelector('.tube-img')).backgroundImage")
    assert "blob:" in bg
    # no horizontal overflow of the page at this viewport
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 1")


# ---------------------------------------------------------------- b. edit + save


def test_edit_text_save_writes_file(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    edit_tube0_text(page, "Edited text")
    assert page.locator("#saveBtn").is_enabled()
    save(page)
    data = load_screens_file(screens_file)
    assert data["screens"]["main"]["tubes"][0]["text"] == "Edited text"


# ---------------------------------------------------------------- c. undo / redo


def test_undo_redo_buttons_and_shortcuts(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    edit_tube0_text(page, "Edited text")
    assert page.evaluate("state.data.screens.main.tubes[0].text") == "Edited text"

    # Ctrl+Z outside an input reverts the edit
    page.click("#screenTitle")          # move focus out of the text field
    page.keyboard.press("Control+z")
    page.wait_for_function(
        "state.data.screens.main.tubes[0].text === 'Hello tubes'")
    # Ctrl+Shift+Z reapplies it
    page.keyboard.press("Control+Shift+z")
    page.wait_for_function(
        "state.data.screens.main.tubes[0].text === 'Edited text'")
    # the buttons do the same
    page.click("#undoBtn")
    page.wait_for_function(
        "state.data.screens.main.tubes[0].text === 'Hello tubes'")
    page.click("#redoBtn")
    page.wait_for_function(
        "state.data.screens.main.tubes[0].text === 'Edited text'")


# ---------------------------------------------------------------- d. history


def test_history_view_and_restore(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    edit_tube0_text(page, "Edited text")
    save(page)

    page.click("#historyBtn")
    page.wait_for_selector("#historyModal:not(.hidden)")
    page.wait_for_function(
        "document.querySelectorAll('#historyList .history-item').length === 1")

    # View shows the YAML of that version (the pre-save content)
    page.locator("#historyList .history-item button", has_text="View").click()
    page.wait_for_selector("#historyYamlView:not(.hidden)")
    page.wait_for_function(
        "document.getElementById('historyYaml').textContent.includes('Hello tubes')")
    page.click("#historyBack")
    page.wait_for_selector("#historyListView:not(.hidden)")

    # Restore brings the old text back (in the page and on disk)
    page.locator("#historyList .history-item button", has_text="Restore").click()
    page.wait_for_function(
        "state.data.screens.main.tubes[0].text === 'Hello tubes'")
    page.wait_for_selector("#historyModal", state="hidden")
    data = load_screens_file(screens_file)
    assert data["screens"]["main"]["tubes"][0]["text"] == "Hello tubes"


# ---------------------------------------------------------------- e. night mode


def test_night_mode_saved_with_defaults_and_quoted_times(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    page.check("#nightEnabled")
    page.wait_for_selector("#nightFields:not(.hidden)")
    page.check("#nightQuiet")           # quiet hours: from/to become quoted strings
    page.wait_for_selector("#quietFields:not(.hidden)")
    save(page)
    data = load_screens_file(screens_file)
    night = data["night"]
    assert night["brightness_day"] == 255
    assert night["brightness_night"] == 64
    assert night["bright_above"] == 3
    assert night["dim_below"] == -6
    assert "dim_screens" not in night           # one night brightness: the clock's own
    assert night["quiet"] == {"from": "23:00", "to": "07:00", "mode": "clock"}
    raw = read_screens_file(screens_file)
    assert '"23:00"' in raw and '"07:00"' in raw   # quoted, not sexagesimal ints


def test_brightness_card_speaks_percent_and_merges_the_two_dimmers(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    page.check("#nightEnabled")
    page.wait_for_selector("#nightFields:not(.hidden)")
    assert page.input_value("#nightDay") == "100" and page.input_value("#nightNight") == "25"
    assert page.input_value("#nightWhen") == "twilight"
    page.wait_for_selector("#nightCustom", state="hidden")
    # darker than the clock's lowest brightness (4%): the pictures dim the rest
    page.fill("#nightNight", "2")
    page.dispatch_event("#nightNight", "input")
    page.select_option("#nightWhen", "early")
    save(page)
    night = load_screens_file(screens_file)["night"]
    assert night["brightness_night"] == 10 and night["dim_screens"] == 0.51
    assert night["bright_above"] == 10 and night["dim_below"] == 0
    page.select_option("#nightWhen", "custom")
    page.wait_for_selector("#nightCustom:not(.hidden)")


# ---------------------------------------------------------------- f. clock picker


def test_clock_picker_own_rotation(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    page.wait_for_selector("#clockSelectRow:not(.hidden)")   # two clocks
    page.select_option("#clockSelect", "kitchen")
    page.wait_for_function(
        "document.getElementById('clockOverrideBtn').textContent"
        ".includes('Give kitchen its own rotation')")
    page.click("#clockOverrideBtn")
    page.wait_for_function(
        "state.data.clocks && state.data.clocks.kitchen"
        " && Array.isArray(state.data.clocks.kitchen.rotation)")
    save(page)
    data = load_screens_file(screens_file)
    assert data["clocks"]["kitchen"]["rotation"] == ["main", "custom", "strip", "clock"]


# ---------------------------------------------------------------- g. gallery


def test_gallery_add_clock_screen(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    page.click("#addScreen")
    page.wait_for_selector("#galleryModal:not(.hidden)")
    page.wait_for_function(
        "document.querySelectorAll('#galleryGrid .gallery-card').length > 1")
    # pick the "Clock" template (no inputs) and add it
    page.locator("#galleryGrid .gallery-card .g-name:text-is('Clock')").click()
    page.wait_for_selector("#galleryFormView:not(.hidden)")
    page.click("#galleryAdd")
    page.wait_for_selector("#galleryModal", state="hidden")
    # the new screen is selected and in the rotation
    page.wait_for_function("state.selectedScreen === 'Clock'")
    page.wait_for_function("state.data.rotation.includes('Clock')")
    assert page.locator("#screenList .screen-item.selected").inner_text() == "Clock"
    assert page.locator("#rotationList .rot-name:text-is('Clock')").count() == 1


# ---------------------------------------------------------------- h. import / export


IMPORT_YAML = """\
imported screen:
  tubes:
    - {type: text, text: "Imported!"}
    - {type: blank}
    - {type: blank}
    - {type: blank}
    - {type: blank}
    - {type: blank}
"""


def test_import_and_export(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    page.click("#importScreens")
    page.wait_for_selector("#importModal:not(.hidden)")
    page.fill("#importText", IMPORT_YAML)
    page.click("#importDo")
    page.wait_for_selector("#importModal", state="hidden")
    # the imported screen appears in the list, selected, and in the rotation
    page.wait_for_function("state.selectedScreen === 'imported screen'")
    page.wait_for_function("state.data.rotation.includes('imported screen')")
    assert page.locator("#screenList li:text-is('imported screen')").count() == 1

    # export of that screen shows YAML containing its name
    page.click("#exportScreen")
    page.wait_for_selector("#exportModal:not(.hidden)")
    page.wait_for_function(
        "document.getElementById('exportYaml').textContent.includes('imported screen')")


# ---------------------------------------------------------------- i. condition builder


def test_when_builder_applies_template(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    page.click("#whenBuild")
    page.wait_for_selector("#whenModal:not(.hidden)")
    page.click("#whenAddCond")
    page.wait_for_function(
        "document.querySelectorAll('#whenConditions .when-cond').length === 2")
    row = page.locator("#whenConditions .when-cond").first
    row.locator(".wc-entity").fill("sensor.x")
    row.locator(".wc-op").select_option("above")
    row.locator(".wc-value").fill("20")
    page.click("#whenApply")
    page.wait_for_selector("#whenModal", state="hidden")
    page.wait_for_function(
        "document.getElementById('scrWhen').value.includes('> 20')")
    assert "> 20" in page.evaluate("state.data.screens.main.when")


# ---------------------------------------------------------------- j. canvas designer


def test_canvas_designer_tube_lines_and_drag(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    select_screen(page, "canvas")
    # the designer shows the canvas with the six tube boundary lines and the items
    page.wait_for_selector("#tubeEditor .designer .designer-stage.wide")
    page.wait_for_function(
        "document.querySelectorAll('.designer-overlay .tube-line').length === 5")
    page.wait_for_function(
        "document.querySelectorAll('.designer-overlay .item-box').length === 3")
    assert page.locator(".designer-side .item-list .item-row").count() == 3

    # dragging the first item changes its x in the state
    before = page.evaluate("state.data.screens.canvas.canvas.items[0].x")
    item = page.locator(".designer-overlay .item-box").first
    item.scroll_into_view_if_needed()   # on a phone the designer is below the fold
    box = item.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 80, y, steps=5)
    page.mouse.up()
    page.wait_for_function(
        "(prev) => state.data.screens.canvas.canvas.items[0].x !== prev", arg=before)
    assert page.locator("#saveBtn").is_enabled()


# ---------------------------------------------------------------- k. fonts


def test_font_select_lists_bundled_families(page, screens_file):
    page, base_url = page
    open_editor(page, base_url)
    page.wait_for_function(
        "document.getElementById('defaultFont').options.length > 5")
    names = page.eval_on_selector_all(
        "#defaultFont option", "els => els.map(e => e.textContent)")
    assert "Bebas Neue" in names and "Orbitron" in names and "Default" in names

    # choosing one keeps the preview working (no broken tube, no error status)
    old_preview = page.evaluate("state.previewUrl")
    page.select_option("#defaultFont", "bebas-neue")
    page.wait_for_function(
        "(prev) => state.previewUrl !== null && state.previewUrl !== prev",
        arg=old_preview)
    assert page.locator(".tube-img.broken").count() == 0
    assert "error" not in page.evaluate(
        "document.getElementById('status').className")
