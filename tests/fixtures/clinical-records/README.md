# Clinical-records fixtures

**Everything in this folder is synthetic.** Every value is invented. No file
here contains, or is derived from, anyone's real health data. Only the shape
mirrors the FHIR JSON that an Apple Health export places in
`clinical-records/`, as sent by an Epic-style source. See `../README.md` for
the same rule applied to the rest of the fixtures.

Strings containing `SHOULD-NOT-BE-STORED` or `SHOULD-NOT-BE-FETCHED` are
identity or attachment markers. The parser must never keep them, and the tests
assert that it does not.

| File | Purpose |
| --- | --- |
| `Observation-lab-chol.json` | A laboratory result with a reference range and a flagless value. Carries subject, encounter and performer. |
| `Observation-lab-ldl-norange.json` | A lab with a value and no reference range. |
| `Observation-lab-text.json` | A lab with only a string value. |
| `Observation-vital-pulse.json`, `Observation-vital-spo2.json` | Vital-sign observations. |
| `Observation-imaging.json` | An observation with an unexpected category, kept as a vital. |
| `Condition-knee.json` | Condition with status, onset and SNOMED coding. |
| `MedicationRequest-ibuprofen.json` | Medication named through a contained resource, with dosage text. |
| `AllergyIntolerance-pollen.json`, `Immunization-flu.json`, `Procedure-acl.json` | One fact each. |
| `DocumentReference-progress.json`, `DiagnosticReport-lipid.json` | Note references: type and date kept, content and URL dropped. |
| `Patient-me.json` | Identity only. Never parsed. |
| `malformed.json` | Truncated, invalid JSON. |
