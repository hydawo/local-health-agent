"""Local corpus of medical literature, kept strictly apart from personal data.

The separation from `store/` is structural, not stylistic. A shared chunk table
would let a PubMed abstract be retrieved and hydrated with a citation that reads
like a citation to the user's own medical record — a fabrication this package's
existence prevents by construction rather than by a WHERE clause.
"""
