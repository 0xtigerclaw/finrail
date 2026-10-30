"""Issuer profiles and method rules are data files; the loader is the contract.

A profile file that drifts from the schema must refuse at load with the file and the
key named, because a silently ignored key would turn into a silently wrong figure.
"""
import copy
import json
import shutil

import pytest

from finrail.core.errors import ContractViolation
from finrail.extensions.finance import public_reports as r


def _dir(tmp_path):
    out = tmp_path / "profiles"
    shutil.copytree(r.PROFILES_DIR, out)
    return out


def _edit(directory, name, change):
    file = directory / name
    doc = json.loads(file.read_text())
    change(doc)
    file.write_text(json.dumps(doc))


def test_shipped_profiles_load_with_the_shapes_the_checker_reads():
    profiles, rules = r.load_profiles()
    assert set(profiles) == {"NVIDIA", "Anthropic"} and profiles == r.PROFILES and rules == r.HOUSE_RULES
    assert list(profiles["NVIDIA"]["tables"]) == [5, 6] and all(isinstance(n, int) for n in profiles["Anthropic"]["tables"])
    assert profiles["NVIDIA"]["tables"][6]["header"] == ("Three Months Ended", "$ in millions")
    for prof in profiles.values():
        assert prof["version"] == 1 and prof["default"] in prof["definitions"]
        for spec in prof["definitions"].values():
            assert all(sign in "+-" and key in prof["rows"] for sign, key in spec["terms"])
    assert profiles["Anthropic"]["edgar_cik"] == "0002130357"
    assert profiles["Anthropic"]["rows"]["aoi_settlement"]["blank_is_zero"] is True
    assert rules["M-1"]["illustrative"] is True and "never added back" in rules["M-1"]["text"]
    assert r.ROWS["operating_cash_flow"] == "Net cash provided by operating activities"


@pytest.mark.parametrize("name,change,message", [
    ("NVIDIA.json", lambda d: d.update(colour="blue"), "NVIDIA.json: unknown key 'colour' in the profile"),
    ("NVIDIA.json", lambda d: d.pop("query"), "NVIDIA.json: missing required key 'query' in the profile"),
    ("NVIDIA.json", lambda d: d["rows"]["reported_fcf"].update(tolerance=1), "unknown key 'tolerance' in row reported_fcf"),
    ("NVIDIA.json", lambda d: d["rows"]["reported_fcf"].pop("sign"), "missing required key 'sign' in row reported_fcf"),
    ("NVIDIA.json", lambda d: d["definitions"]["issuer_fcf"].update(tolerence=1), "unknown key 'tolerence' in definition issuer_fcf"),
    ("NVIDIA.json", lambda d: d["definitions"]["issuer_fcf"].update(tolerance=-1), "tolerance -1 must be zero or a positive"),
    ("NVIDIA.json", lambda d: d["definitions"]["issuer_fcf"].update(reconcile="nope"), "reconciles to 'nope', which is not a row"),
    ("NVIDIA.json", lambda d: d["definitions"]["issuer_fcf"]["terms"].append(["+", "nope"]), "is not"),
    ("NVIDIA.json", lambda d: d["rows"]["reported_fcf"].update(table=7), "names table 7, which the profile does not declare"),
    ("NVIDIA.json", lambda d: d.update(version=2), "version 2 is not one this code reads"),
    ("NVIDIA.json", lambda d: d.update(company="Nvidia"), "does not match the file name"),
    ("NVIDIA.json", lambda d: d.update(default="revenue"), "default 'revenue' is not one of the definitions"),
    ("NVIDIA.json", lambda d: d["tables"]["5"].pop("header"), "missing required key 'header' in table 5"),
    ("Anthropic.json", lambda d: d["column"].pop("period"), "missing required key 'period' in column (kind 'under')"),
    ("Anthropic.json", lambda d: d.update(refused={"adjusted_operating_income": "M-9"}), "names method rule 'M-9', which house_rules.json does not declare"),
    ("house_rules.json", lambda d: d["rules"]["M-1"].pop("refusal"), "house_rules.json: missing required key 'refusal' in rule M-1"),
    ("house_rules.json", lambda d: d["rules"]["M-1"].update(owner="x"), "house_rules.json: unknown key 'owner' in rule M-1"),
])
def test_a_profile_that_drifts_from_the_schema_refuses_naming_file_and_key(tmp_path, name, change, message):
    directory = _dir(tmp_path)
    _edit(directory, name, change)
    with pytest.raises(ContractViolation) as caught:
        r.load_profiles(directory)
    assert message in str(caught.value)


def test_a_stray_or_unreadable_file_in_the_profile_directory_refuses(tmp_path):
    directory = _dir(tmp_path)
    (directory / "notes.txt").write_text("x")
    with pytest.raises(ContractViolation, match="notes.txt: only .json"):
        r.load_profiles(directory)
    (directory / "notes.txt").unlink()
    (directory / "NVIDIA.json").write_text("{not json")
    with pytest.raises(ContractViolation, match="NVIDIA.json: could not be read as JSON"):
        r.load_profiles(directory)
    with pytest.raises(ContractViolation, match="does not exist"):
        r.load_profiles(tmp_path / "missing")


def test_a_new_issuer_file_is_picked_up_without_a_code_change(tmp_path):
    directory = _dir(tmp_path)
    doc = json.loads((directory / "NVIDIA.json").read_text())
    doc["company"] = "Acme"
    doc["definitions"]["issuer_fcf"]["tolerance"] = 1
    (directory / "Acme.json").write_text(json.dumps(doc))
    profiles, _rules = r.load_profiles(directory)
    assert set(profiles) == {"NVIDIA", "Anthropic", "Acme"}
    assert profiles["Acme"]["definitions"]["issuer_fcf"]["tolerance"] == 1
    assert copy.deepcopy(profiles["NVIDIA"]) == r.PROFILES["NVIDIA"]
