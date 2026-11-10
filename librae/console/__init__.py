"""Read-only Streamlit console over the engine tables.

Install the ``console`` extra, point ``LIBRAE_CONSOLE_DSN`` at a read-only
role, and start it with ``python -m librae.console`` (arguments after it go to
``streamlit run``). Nothing here writes to the database.
"""
