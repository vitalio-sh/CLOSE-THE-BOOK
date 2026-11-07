"""Deterministic bounded exact subset search; no amount-only global lookup."""
from collections import defaultdict


def subset_solutions(values, target, *, minimum=1, maximum_items=24):
    """Return up to two solutions, or None when the item budget is exceeded.

    Integer minor units only. Meet in the middle limits each half to 4096
    subsets at the default cap. Distinct equal-valued items remain ambiguous.
    """
    if len(values) > maximum_items:
        return None
    if not values:
        return []
    middle = len(values) // 2
    def sums(start, stop):
        result = [(0, ())]
        for index in range(start, stop):
            result += [(total + values[index], ids + (index,)) for total, ids in result]
        return result
    right = defaultdict(list)
    for total, ids in sums(middle, len(values)):
        right[total].append(ids)
    found = []
    for total, left in sums(0, middle):
        for tail in right.get(target - total, ()):
            ids = left + tail
            if len(ids) < minimum:
                continue
            found.append(ids)
            if len(found) == 2:
                return found
    return found


def positive_subsets(values, target, *, limit=2, maximum_items=30):
    """Up to `limit` index tuples of strictly positive integer values summing to target.

    Meet in the middle; partial sums above the target are pruned. Returns None when
    the item budget is exceeded, so callers abstain instead of guessing.
    """
    if len(values) > maximum_items or target <= 0 or any(v <= 0 for v in values):
        return None
    middle = len(values) // 2
    def sums(start, stop):
        result = [(0, ())]
        for index in range(start, stop):
            value = values[index]
            result += [(total + value, ids + (index,)) for total, ids in result if total + value <= target]
        return result
    right = defaultdict(list)
    for total, ids in sums(middle, len(values)):
        right[total].append(ids)
    found = []
    for total, left in sums(0, middle):
        for tail in right.get(target - total, ()):
            found.append(left + tail)
            if len(found) >= limit:
                return found
    return found


def unique_subset(values, target, *, minimum=1, maximum_items=24):
    """Return the sole index tuple; abstain on ambiguity or budget exhaustion."""
    found = subset_solutions(values, target, minimum=minimum, maximum_items=maximum_items)
    return found[0] if found is not None and len(found) == 1 else None
