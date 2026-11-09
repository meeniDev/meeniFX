"""The contextual Roto Brush toolbar: required actions, state and preview modes."""

from meenifx.ui.preview import PreviewPanel

PREVIEWS = {
    "original": "Original",
    "outline": "Outline",
    "isolated": "Isolated Subject",
    "alpha": "Alpha Matte",
    "checkerboard": "Transparency Checkerboard",
    "composite": "Composite",
}


def test_toolbar_has_every_workspace_action_and_preview_mode(qapp):
    panel = PreviewPanel(lambda: 100)
    bar = panel.roto_bar
    try:
        assert bar.isHidden(), "the workspace toolbar stays out of the normal editor"
        panel.set_roto_workspace(True)
        assert not bar.isHidden(), "entering Roto Brush shows the contextual toolbar"
        tips = " ".join([bar.done_button.text(),
            bar.fg_button.toolTip(), bar.bg_button.toolTip(), bar.refine_button.toolTip(),
            bar.subject_button.toolTip(), bar.size_spin.toolTip(),
            bar.undo_button.toolTip(), bar.redo_button.toolTip(),
            bar.back_button.toolTip(), bar.forward_button.toolTip(),
            bar.analyze_button.toolTip(), bar.freeze_button.toolTip(),
            bar.preview_combo.toolTip(), bar.done_button.toolTip(),
            bar.model_button.toolTip(),
        ])
        for phrase in ("Foreground Brush", "Background Brush", "Refine Edge",
                       "Choose Subject", "Brush radius", "Undo Stroke", "Redo Stroke",
                       "Propagate Backward", "Propagate Forward", "Analyze Clip",
                       "Freeze", "Export always uses Composite", "Done", "AI model"):
            assert phrase.lower() in tips.lower(), f"missing toolbar action: {phrase}"

        assert [bar.preview_combo.itemData(index)
                for index in range(bar.preview_combo.count())] == list(PREVIEWS)
        assert [bar.preview_combo.itemText(index)
                for index in range(bar.preview_combo.count())] == list(PREVIEWS.values())
    finally:
        panel.close()


def test_toolbar_controls_emit_the_real_workspace_actions(qapp):
    panel = PreviewPanel(lambda: 100)
    bar = panel.roto_bar
    try:
        tools, sizes, previews, freezes = [], [], [], []
        bar.tool_changed.connect(tools.append)
        bar.brush_size_changed.connect(sizes.append)
        bar.preview_changed.connect(previews.append)
        bar.freeze_changed.connect(freezes.append)

        bar.bg_button.click()
        assert tools == ["background"]
        bar.refine_button.click()
        assert tools[-1] == "refine"
        bar.size_spin.setValue(12)
        assert sizes[-1] == 0.12
        bar.preview_combo.setCurrentIndex(bar.preview_combo.findData("isolated"))
        assert previews[-1] == "isolated"
        bar.freeze_button.click()
        assert freezes[-1] is True
    finally:
        panel.close()


def test_toolbar_state_shows_progress_backend_and_running_controls(qapp):
    panel = PreviewPanel(lambda: 100)
    bar = panel.roto_bar
    try:
        bar.set_state(7, 20, True, "alpha", "u2net", True,
                      "U^2-Net ready", "U^2-Net 168 MB, CPU inference")
        assert bar.preview_combo.currentData() == "alpha"
        assert bar.freeze_button.isChecked()
        assert "7/20" in bar.status.text() and "AI" in bar.status.text()
        assert bar.stop_button.isVisibleTo(bar)
        assert not bar.analyze_button.isVisibleTo(bar)

        bar.set_state(20, 20, False, "composite", "basic", False,
                      "AI model not installed", "Download the optional model")
        assert bar.preview_combo.currentData() == "composite"
        assert not bar.freeze_button.isChecked()
        assert "Basic" in bar.status.text()
        assert bar.analyze_button.isVisibleTo(bar)
        assert not bar.stop_button.isVisibleTo(bar)
    finally:
        panel.close()
