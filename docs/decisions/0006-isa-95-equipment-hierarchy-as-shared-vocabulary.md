# 0006. ISA-95 equipment hierarchy, owned by the ERP, shared by the graph and the UNS

- **Status:** Accepted, 2026-11-10 (before Wave 5)

## Context

The knowledge graph (Wave 4b) was built before the Unified Namespace (Wave 6). The two depend on
each other: the graph gives UNS topics their meaning, and the UNS keeps the graph's digital twin
live. They can only work together if they name the factory the same way.

The first draft of this decision used "company code → plant → area → line → work centre". That is
wrong for ISA-95 and was corrected before any code was written. In ISA-95 (IEC 62264), a **work
centre** is the level directly under an area, and a production line is one *kind* of work centre;
a **work unit** is the level below it, and a work cell is one kind of work unit. The confusion came
from SAP, where a "work centre" is the place a routing operation runs, which in ISA-95 terms is
usually a work unit.

## Decision

- The ERP owns the equipment hierarchy as master data, built in Wave 5:

  | ISA-95 level (type) | ERP | Graph label | UNS segment |
  |---|---|---|---|
  | Enterprise | Company code | `CompanyCode` | `1000` |
  | Site | Plant | `Plant` | `1000` |
  | Area | Production area | `Area` | `assembly` |
  | Work centre (production line) | Production line | `Line` | `line-1` |
  | Work unit (work cell) | Work centre (SAP sense): routing operations, activity costs, cost centre | `Cell` | `press-3` |

- The ERP keeps SAP's term "work centre" on its screens and API, and each record also stores its
  ISA-95 level. The graph and the UNS use only ISA-95 names.
- The graph links `Cell -PART_OF-> Line -PART_OF-> Area -PART_OF-> Plant` and
  `Cell -CHARGED_TO-> CostCenter`. Every equipment node carries its ISA-95 level and its `uns_path`.
- UNS topic segments are the same lower-case business codes, never display names.

## Alternatives considered

- **Let the UNS define the hierarchy and the ERP follow:** common in factories that start from the
  shop floor. Here the ERP comes first and already owns plants and cost centres, so splitting
  ownership would create two masters.
- **Use only SAP terms everywhere:** simpler for the ERP, but MES and UNS practitioners think in
  ISA-95 terms and would misread "work centre".

## Consequences

- Wave 6 plugs the UNS into names that already exist in the ERP and the graph.
- A machine hour from the shop floor can be followed from the cell to its cost centre and profit
  centre in one graph traversal.
- Renaming a line's description never breaks a subscription; changing its code is a master-data
  change that must be planned.
