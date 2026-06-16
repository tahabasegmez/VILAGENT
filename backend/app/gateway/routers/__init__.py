"""Gateway router package.

Router modules are imported explicitly by ``app.gateway.app`` and consumers.
Keeping this package initializer empty avoids loading the entire Gateway when a
single router is imported for tests or embedded use.
"""
