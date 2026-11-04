"""Tiny observable values for model state; deliberately independent of UI toolkits."""


class Value:
    def __init__(self, value):
        self._value = value
        self._callbacks = []

    def get(self):
        return self._value

    def set(self, value):
        if value == self._value:
            return
        self._value = value
        for callback in tuple(self._callbacks):
            callback("", "", "write")

    def trace_add(self, mode, callback):
        if callback not in self._callbacks:
            self._callbacks.append(callback)
        return callback

    def trace_remove(self, mode, callback):
        if callback in self._callbacks:
            self._callbacks.remove(callback)
