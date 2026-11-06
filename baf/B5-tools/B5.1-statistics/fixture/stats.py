import csv
import statistics
import sys

with open(sys.argv[1], newline="", encoding="utf-8") as handle:
    values = [float(row["value"]) for row in csv.DictReader(handle)]
print(f"count: {len(values)}")
print(f"mean: {statistics.mean(values)}")
print(f"median: {statistics.median(values)}")
