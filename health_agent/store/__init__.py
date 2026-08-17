"""Local stores.

`sqlite_schema` owns the structured store (HealthKit records, workouts, daily
aggregates). `queries` is the read layer the CLI uses today and the agent's tools
will use at milestone 5. `vector_store` (notes/records embeddings) arrives at
milestones 2-3.
"""
