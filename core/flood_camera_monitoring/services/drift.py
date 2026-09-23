"""Distribution and reviewed-disagreement diagnostics; not a precision estimate."""
import math


def distribution_shift(current, baseline, minimum=30):
    if len(current) < minimum or len(baseline) < minimum:
        return {'status': 'INSUFFICIENT_DATA', 'score': None}
    def histogram(values):
        bins = [1.0]*10
        for value in values:
            bins[min(9, max(0, int(value)//10))] += 1
        total = sum(bins)
        return [value/total for value in bins]
    first, second = histogram(current), histogram(baseline)
    midpoint = [(a+b)/2 for a, b in zip(first, second)]
    score = sum(a*math.log2(a/m) + b*math.log2(b/m) for a, b, m in zip(first, second, midpoint))/2
    return {'status': 'SHIFT' if score >= .1 else 'STABLE', 'score': score,
        'current_count': len(current), 'baseline_count': len(baseline)}
