"""Production planning (SAP PP): the factory's structure and how products are made.

Wave 5a holds the master data: the ISA-95 equipment hierarchy (areas, lines, work centres), activity
types and their rates, bills of materials and routings, plus a standard cost estimate. Production
orders, which consume this data, follow in Wave 5b. See ``docs/decisions/0006`` for why the ERP owns
the equipment hierarchy and how its codes become Unified Namespace topic paths.
"""
