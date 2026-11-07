"""Knowledge graph: a Neo4j read model of the ERP.

Business changes are written to an outbox in the same transaction, and a Celery worker projects
them into Neo4j for Customer 360, document flow, supplier dependency and GraphRAG retrieval.
"""
