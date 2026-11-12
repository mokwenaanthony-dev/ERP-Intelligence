"""Unified Namespace bridge: the ERP's side of the MES link (Wave 6).

Publishes what the shop floor needs from the ERP (equipment definitions, operation requests of
released orders) to the MQTT broker, and turns the MES's operation-performance events into
confirmations. Everything it sends and accepts is defined in ``docs/UNS_CONTRACT.md``; the MES lives
in its own repository (ADR 0009). Off unless ``MQTT_URL`` is set, like the graph without ``NEO4J_URI``.
"""
