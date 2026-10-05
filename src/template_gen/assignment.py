"""
Small numeric helpers shared by the template generator.

linear_sum_assignment() is the classic Hungarian (Kuhn-Munkres) algorithm with
potentials, O(n^2 m), written with numpy so scipy is not a dependency. It
matches scipy.optimize.linear_sum_assignment for rectangular cost matrices.
"""

import numpy as np


def linear_sum_assignment(cost, maximize=False):
    """Return (row_indices, col_indices) of the minimum (or maximum) cost matching."""
    cost = np.asarray(cost, dtype=np.float64)
    if cost.ndim != 2:
        raise ValueError("cost must be a 2-D matrix")
    if cost.size == 0:
        return np.zeros(0, int), np.zeros(0, int)
    if maximize:
        cost = -cost
    transposed = cost.shape[0] > cost.shape[1]
    if transposed:
        cost = cost.T
    n, m = cost.shape
    u = np.zeros(n + 1)
    v = np.zeros(m + 1)
    p = np.zeros(m + 1, dtype=int)  # p[j]: row (1-based) matched to column j
    way = np.zeros(m + 1, dtype=int)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = np.full(m + 1, np.inf)
        used = np.zeros(m + 1, dtype=bool)
        while True:
            used[j0] = True
            i0 = p[j0]
            free = ~used[1:]
            current = cost[i0 - 1] - u[i0] - v[1:]
            better = free & (current < minv[1:])
            minv[1:][better] = current[better]
            way[1:][better] = j0
            candidates = np.where(free, minv[1:], np.inf)
            j1 = int(np.argmin(candidates)) + 1
            delta = candidates[j1 - 1]
            used_cols = np.nonzero(used)[0]
            u[p[used_cols]] += delta
            v[used_cols] -= delta
            minv[1:][free] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    cols = np.nonzero(p[1:])[0]
    rows = p[1:][cols] - 1
    if transposed:
        rows, cols = cols, rows
    order = np.argsort(rows)
    return rows[order], cols[order]


def cluster_1d(values, tolerance):
    """Greedy 1-D clustering of sorted values; returns (centres, labels)."""
    values = np.asarray(values, dtype=np.float64)
    if len(values) == 0:
        return np.zeros(0), np.zeros(0, int)
    order = np.argsort(values)
    labels = np.zeros(len(values), dtype=int)
    centres = []
    members = [order[0]]
    for index in order[1:]:
        if values[index] - np.mean(values[members]) <= tolerance:
            members.append(index)
        else:
            centres.append(np.mean(values[members]))
            labels[members] = len(centres) - 1
            members = [index]
    centres.append(np.mean(values[members]))
    labels[members] = len(centres) - 1
    return np.array(centres), labels


def otsu_1d(values, bins=256):
    """Otsu's threshold for an arbitrary 1-D sample."""
    values = np.asarray(values, dtype=np.float64)
    if len(values) < 2 or values.max() <= values.min():
        return float(values.mean()) if len(values) else 0.0
    hist, edges = np.histogram(values, bins=bins)
    centres = (edges[:-1] + edges[1:]) / 2
    weight0 = np.cumsum(hist)
    weight1 = weight0[-1] - weight0
    sum0 = np.cumsum(hist * centres)
    mean0 = sum0 / np.maximum(weight0, 1)
    mean1 = (sum0[-1] - sum0) / np.maximum(weight1, 1)
    between = weight0 * weight1 * (mean0 - mean1) ** 2
    # Empty bins between the classes tie; take the middle of the plateau
    best = np.nonzero(between >= between.max() * (1 - 1e-9))[0]
    return float((edges[best[0] + 1] + edges[best[-1] + 1]) / 2)


def odd(value, minimum=3):
    value = max(int(round(value)), minimum)
    return value if value % 2 else value + 1


def boxes_overlap(a, b, pad=0):
    """Axis-aligned overlap test for [x, y, w, h] boxes."""
    return not (
        a[0] + a[2] + pad <= b[0]
        or b[0] + b[2] + pad <= a[0]
        or a[1] + a[3] + pad <= b[1]
        or b[1] + b[3] + pad <= a[1]
    )


def point_in_box(x, y, box, pad=0):
    return (
        box[0] - pad <= x <= box[0] + box[2] + pad
        and box[1] - pad <= y <= box[1] + box[3] + pad
    )
