import json
from verify_yaml import load, resolve_plain, parse_scalar

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label} {detail}")
        failures.append(label)


# --- sanity check of the resolver itself: proves unquoted forms WOULD be coerced
check("resolver: plain NO -> bool False", resolve_plain("NO") is False, repr(resolve_plain("NO")))
check("resolver: plain ON -> bool True", resolve_plain("ON") is True, repr(resolve_plain("ON")))
check("resolver: plain 0123 -> int", resolve_plain("0123") == 83, repr(resolve_plain("0123")))
check("resolver: plain 0042 -> int", resolve_plain("0042") == 34, repr(resolve_plain("0042")))
check('resolver: quoted "NO" -> str', parse_scalar('"NO"') == "NO", repr(parse_scalar('"NO"')))
check('resolver: quoted "0123" -> str', parse_scalar('"0123"') == "0123", repr(parse_scalar('"0123"')))

# --- negative test: the SAME documents WITHOUT quotes get coerced (why quoting matters)
bad_c = load('countries:\n  - NO\n  - ON\n  - FR\n')
bad_p = load('postal_codes:\n  central: 0123\n  west: 0042\n')
check('negative: unquoted countries are NOT all str (coerced)',
      not all(type(v) is str for v in bad_c['countries']), bad_c['countries'])
check('negative: unquoted postal codes are NOT all str (coerced)',
      not all(type(v) is str for v in bad_p['postal_codes'].values()), bad_p['postal_codes'])
print('   unquoted would give:', bad_c, bad_p)

# --- countries.yaml
c = load(open("countries.yaml", encoding="utf-8").read())
print("\ncountries.yaml ->", c)
check("countries: single top-level key 'countries'", list(c.keys()) == ["countries"], c.keys())
check("countries: ordered list NO, ON, FR", c["countries"] == ["NO", "ON", "FR"], c["countries"])
check("countries: every element is str", all(type(v) is str for v in c["countries"]),
      [(v, type(v).__name__) for v in c["countries"]])

# --- postal.yaml
p = load(open("postal.yaml", encoding="utf-8").read())
print("postal.yaml    ->", p)
check("postal: single top-level key 'postal_codes'", list(p.keys()) == ["postal_codes"], p.keys())
codes = p["postal_codes"]
check("postal: exactly keys central, west", list(codes.keys()) == ["central", "west"], codes.keys())
check("postal: central == '0123'", codes.get("central") == "0123", repr(codes.get("central")))
check("postal: west == '0042'", codes.get("west") == "0042", repr(codes.get("west")))
check("postal: every value is str", all(type(v) is str for v in codes.values()),
      [(v, type(v).__name__) for v in codes.values()])

print("\n--- round-trip as JSON (string types visible) ---")
print(json.dumps({"countries": c["countries"], "postal_codes": codes}, indent=2, ensure_ascii=False))

print("\nRESULT:", "ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
