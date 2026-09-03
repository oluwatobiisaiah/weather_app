"""Tkinter presentation layer.

Nothing in this package calls `requests` or touches the filesystem directly --
it asks the core services to do that, always from a worker thread.
"""
