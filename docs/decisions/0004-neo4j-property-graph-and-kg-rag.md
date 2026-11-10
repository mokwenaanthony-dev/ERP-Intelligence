# 0004. Neo4j property graph with an explicit ontology; KG-RAG, not GraphRAG

- **Status:** Accepted (Wave 4b), revised before Wave 5 on 2026-11-10
- **Revision:** terminology corrected and the ontology made explicit; nothing built changed

## Context

The graph serves Customer 360, document flow, supplier dependency and grounded context for AI
agents. A review before Wave 5 asked whether the project uses "knowledge graph", "semantic layer"
and "GraphRAG" correctly.

## Decision

- **Neo4j 5.26 LTS Community, labelled property graph.** Chosen over Apache AGE for its tooling
  (Browser, Cypher ecosystem) and recognition. A property graph is a recognised kind of knowledge
  graph; RDF/OWL is not required for this use.
- **An explicit ontology.** A graph is a *semantic* layer only if each node type and relationship
  has a defined meaning. The ontology is therefore documented in `docs/ONTOLOGY.md` (node types,
  keys, properties, relationships, source tables, version), not only implied by the projector
  code. Neo4j enforces a uniqueness constraint on the business key of every label (built in 4b).
- **Stable business keys** (partner and material numbers, plant codes, `company/document number`),
  the same keys the MES and the Unified Namespace use, so outside data joins without mapping tables.
- **"KG-RAG", not "GraphRAG".** Microsoft's GraphRAG has a language model extract a graph from
  unstructured text and summarise its communities. Here the graph is projected from validated ERP
  records, and retrieval returns permission-filtered facts that each trace back to a document. The
  documentation calls this knowledge-graph retrieval (KG-RAG); the endpoint stays
  `GET /api/graph/context/`.

## Alternatives considered

- **RDF triple store with OWL:** stronger reasoning and standard vocabularies, but less tooling
  familiarity and no need for inference yet. Can be revisited if formal reasoning is needed.
- **Keep the word GraphRAG:** widely used loosely, but an informed reviewer would rightly point out
  the difference.

## Consequences

- Anyone can read what a node or relationship means without reading Python.
- Ontology changes are versioned: a breaking change bumps the version and runs `graph_rebuild --clear`.
- A future agent can still add LLM-extracted facts from documents (true GraphRAG) beside the ERP
  facts, as long as their source is labelled and kept separate.
