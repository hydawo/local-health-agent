from health_agent import labs


def test_loinc_wins_over_an_unrecognized_printed_name():
    """Epic writes CALCULATED LDL; the alias table has never heard of it,
    but the LOINC code is unambiguous."""
    key, panel = labs.resolve_coded([("http://loinc.org", "13457-7")],
                                    "CALCULATED LDL")
    assert key == "ldl"
    assert panel == "lipids"


def test_name_resolution_still_applies_without_a_loinc_code():
    assert labs.resolve_coded([("urn:oid:1.2.3", "CHOL")], "LDL cholesterol")[0] == "ldl"


def test_an_unmapped_coded_analyte_falls_back_to_a_slug():
    key, _ = labs.resolve_coded([("http://loinc.org", "99999-9")], "Zeta protein")
    assert key == labs.resolve_key("Zeta protein")[0]


def test_every_loinc_entry_points_at_a_registered_analyte():
    registered = {a.key for a in labs.REGISTRY}
    for code, key in labs.LOINC_TO_KEY.items():
        assert key in registered, (
            f"{code} maps to {key!r}, which the registry does not define")


def test_standard_and_high_sensitivity_crp_stay_distinct():
    loinc = "http://loinc.org"
    assert labs.resolve_coded([(loinc, "1988-5")], "C-reactive protein") == (
        "crp", "inflammation")
    assert labs.resolve_coded([(loinc, "30522-7")], "C-reactive protein") == (
        "hs_crp", "inflammation")


def test_loinc_codes_are_well_formed():
    for code in labs.LOINC_TO_KEY:
        assert "-" in code and code.replace("-", "").isdigit()
