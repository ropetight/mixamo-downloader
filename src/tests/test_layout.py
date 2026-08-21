"""Tests for the window layout.

The controls sit in three columns under the browser, with the log across the
bottom, and the whole thing is a splitter so the browser can be given as much
of a wide window as the user wants. These tests keep that shape from drifting
back into one tall stack of full-width rows.

The window is built on the offscreen platform with the page load stubbed out,
so nothing here needs a display or the network.
"""

def group_titles(widget):
    """Collect the titles of the group boxes directly inside a widget.

    :param widget: Widget to look through
    :type widget: QtWidgets.QWidget

    :rtype: list
    """
    from PySide6 import QtWidgets

    return [child.title()
            for child in widget.children()
            if isinstance(child, QtWidgets.QGroupBox)]


class TestSplitter:
    """The browser has to be resizable against everything below it."""

    def test_the_window_is_one_vertical_splitter(self, window):
        from PySide6 import QtCore, QtWidgets

        central = window.centralWidget()

        assert isinstance(central, QtWidgets.QSplitter)
        assert central.orientation() == QtCore.Qt.Orientation.Vertical

    def test_it_holds_the_browser_the_controls_and_the_log(self, window):
        assert window.splitter.count() == 3
        assert window.splitter.widget(0) is window.browser

    def test_a_taller_window_gives_the_room_to_the_browser(self, window,
                                                           qt_app):
        # Dragging the window taller should show more of the page, not
        # stretch the dropdowns.
        window.resize(1400, 700)
        window.show()
        qt_app.processEvents()
        before = window.splitter.sizes()

        window.resize(1400, 1100)
        qt_app.processEvents()
        after = window.splitter.sizes()

        assert after[0] - before[0] > 300
        assert abs(after[1] - before[1]) <= 20
        assert abs(after[2] - before[2]) <= 20

    def test_the_browser_starts_with_most_of_the_window(self, window,
                                                        qt_app):
        window.resize(1600, 900)
        window.show()
        qt_app.processEvents()

        sizes = window.splitter.sizes()

        assert sizes[0] > sizes[1] + sizes[2]

    def test_no_pane_can_be_collapsed_to_nothing(self, window):
        # A pane dragged shut cannot be dragged back, which reads as the
        # controls having vanished.
        assert not window.splitter.childrenCollapsible()


class TestColumns:
    """Three columns side by side, which is what a wide window wants."""

    def test_the_controls_are_three_panels_in_a_row(self, window):
        from PySide6 import QtWidgets

        strip = window.splitter.widget(1)
        layout = strip.layout()

        assert isinstance(layout, QtWidgets.QHBoxLayout)
        assert layout.count() == 3

    def test_the_columns_are_what_the_names_say(self, window):
        strip = window.splitter.widget(1)

        assert group_titles(strip) == [
            "What to download", "Download options", "Output folder"]

    def test_the_mode_column_stacks_its_choices(self, window):
        from PySide6 import QtWidgets

        strip = window.splitter.widget(1)
        column = group_titles(strip).index("What to download")
        box = strip.layout().itemAt(column).widget()

        assert isinstance(box.layout(), QtWidgets.QVBoxLayout)

    def test_the_options_column_is_a_labelled_form(self, window):
        from PySide6 import QtWidgets

        form = window.export_box.findChild(QtWidgets.QFormLayout)

        assert form is not None
        assert form.rowCount() == len(window.pref_combos)

    def test_the_dropdowns_fill_their_column(self, window):
        from PySide6 import QtWidgets

        for combo in window.pref_combos.values():
            assert combo.sizePolicy().horizontalPolicy() == \
                QtWidgets.QSizePolicy.Policy.Expanding

    def test_the_run_column_holds_the_path_buttons_and_progress(self, window):
        strip = window.splitter.widget(1)
        column = group_titles(strip).index("Output folder")
        box = strip.layout().itemAt(column).widget()

        for widget in (window.le_path, window.get_btn, window.stop_btn,
                       window.progress_bar, window.lbl_item):
            assert box.isAncestorOf(widget)

    def test_a_long_animation_name_cannot_widen_the_column(self, window):
        from PySide6 import QtWidgets

        # The label is elided by policy rather than pushing the layout out.
        assert window.lbl_item.sizePolicy().horizontalPolicy() == \
            QtWidgets.QSizePolicy.Policy.Ignored


class TestLog:
    """The log spans the full width, below the three columns."""

    def test_it_is_the_bottom_pane(self, window):
        log_pane = window.splitter.widget(2)

        assert log_pane.isAncestorOf(window.log_view)

    def test_it_can_be_folded_away_to_free_room(self, window):
        log_pane = window.splitter.widget(2)

        assert log_pane.isCheckable()

        log_pane.setChecked(False)
        assert not window.log_view.isVisible()

        log_pane.setChecked(True)
        assert window.log_view.isVisibleTo(log_pane)


class TestRememberedLayout:
    """A window someone resized should come back that way."""

    def test_stores_the_geometry_and_the_splitter(self, window):
        window.resize(1440, 810)
        window._store_settings()

        assert window.settings.value("geometry") is not None
        assert window.settings.value("splitter") is not None

    def test_restores_the_splitter_it_stored(self, window):
        window.splitter.setSizes([700, 150, 90])
        stored = window.splitter.saveState()
        window._store_settings()

        window.splitter.setSizes([100, 400, 400])
        window._restore_settings()

        assert window.splitter.saveState() == stored
