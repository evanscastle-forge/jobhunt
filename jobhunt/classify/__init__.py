"""CLASSIFY: decide whether a posting is worth showing, and how relevant it is.

    gates.py      keep or drop: company, title, location. Existence, never order.
    filters.py    measure a posting: distance, travel, pay, clearance, seniority, age
    judge.py      what the model makes of it: eleven signed attributes
    relevance.py  the arithmetic: the dot product, and the grade curve over it
    facts.py      the About-page facts, which are what the model is told
    learn.py      what your own decisions imply: company affinity, duplicate keys

See AGENTS.md for the rules these modules are required to keep.
"""
