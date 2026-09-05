"""Presentation. Reads what the system already computed; computes nothing itself.

This package exists so the dashboard has somewhere to live that is structurally
incapable of becoming a second implementation of the pipeline. Nothing here
plans, judges, executes, or calls a model. It has no import edge into
``pramaan.llm`` and ``tests/test_dashboard_snapshot.py`` asserts that it stays
that way.
"""
