"""Re-style widgets when the theme changes, without keeping them alive.

Most colour comes from the application stylesheet and from painters that read
``tokens.COLORS`` while painting, so a theme switch needs nothing from them. The few widgets
that bake a colour into their own stylesheet or icon register a builder here instead:

    themed_style(label, lambda: f"color: {tokens.COLORS['muted']};")
    bind_theme(button, lambda b: b.setIcon(icon("undo")))

Widgets are held weakly and the builders receive the widget, so a closure never keeps its
own widget alive.
"""

import weakref

_BOUND = weakref.WeakKeyDictionary()
_HOOKS = []


def on_theme_change(hook):
    """Run a module-level ``hook()`` now and before every widget restyle (for shared palettes)."""
    _HOOKS.append(hook)
    hook()
    return hook


def bind_theme(widget, apply):
    """Call ``apply(widget)`` now and again after every theme switch."""
    _BOUND[widget] = apply
    apply(widget)
    return widget


def themed_style(widget, build):
    """``widget.setStyleSheet(build())`` now and after every theme switch."""
    return bind_theme(widget, lambda w: w.setStyleSheet(build()))


def muted_text(widget):
    """Secondary text colour that follows the theme."""
    from .. import tokens
    return themed_style(widget, lambda: f"color: {tokens.COLORS['muted']};")


def warning_text(widget):
    from .. import tokens
    return themed_style(widget, lambda: f"color: {tokens.COLORS['coral_text']};")


def restyle_all():
    for hook in _HOOKS:
        hook()
    for widget, apply in list(_BOUND.items()):
        try:
            apply(widget)
        except RuntimeError:
            pass    # the C++ widget is already gone
