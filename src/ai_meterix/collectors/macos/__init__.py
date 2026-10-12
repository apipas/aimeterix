"""Apple Silicon backend (record 0009 tier 1, record 0011).

Everything is read through ctypes calls into macOS system libraries, without
admin rights and without spawning command-line tools (0011 item 2, 0017).
"""
