"""jobmon - job posting monitor for the 2027 new grad / internship cycle.

Pipeline (spec 3):
    [source adapters] -> [normalizer] -> [dedup store] -> [filters] -> [analyzer] -> [notifier]
"""

__version__ = "0.1.0"
