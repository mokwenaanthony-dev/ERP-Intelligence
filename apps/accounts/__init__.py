"""Accounts: the ERP's user model and its roles.

Users sign in with JWT (``/api/auth/token/``). Each user has one role (Administrator, Finance
Manager, Accountant, Sales Representative, Inventory Manager, Purchasing Officer, HR Admin,
Viewer), and ``permissions.py`` maps each role to what every module's API allows.
"""
