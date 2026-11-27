"""Dell Warranty Sync for Freshservice.

A small desktop tool (Enterprise IT) that compares Freshservice warranty
dates against Dell TechDirect and writes back the differences.

The Dell, Freshservice and sync-engine classes are deliberately decoupled so
they can be reused from the Admin Suite without the UI layer.
"""

__version__ = "1.0"
__build__ = 18
