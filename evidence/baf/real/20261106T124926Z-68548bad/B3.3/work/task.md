# YAML tables

Create countries.yaml as a mapping with only key `countries`, whose value is the following ordered list of strings: `NO`, `ON`, `FR`.
Create postal.yaml as a mapping with only key `postal_codes`, whose value is a mapping: `central` → `0123`, `west` → `0042`.
Use these values exactly, including case and leading zeros. A YAML 1.1 reader must load every code as a string, not as a boolean or number. ON is a synthetic code used by this data source.
