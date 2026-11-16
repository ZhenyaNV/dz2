"""Report values from measured application requests; login is excluded."""
def summarize(data, rate, seconds):
    metrics = data['metrics']
    duration = metrics.get('response_ms', {}).get('values', {})
    errors = metrics.get('request_errors', {}).get('values', {}).get('rate')
    count = metrics.get('requests_done', {}).get('values', {}).get('count', 0)
    p95 = duration.get('p(95)')
    total_seconds = data['state']['testRunDurationMs'] / 1000
    return {'target_rps': rate, 'stage_seconds': seconds,
            'median_ms': duration.get('med'), 'p95_ms': p95, 'max_ms': duration.get('max'),
            'error_fraction': errors, 'completed_requests': count,
            'dropped_iterations': metrics.get('dropped_iterations', {}).get('values', {}).get('count', 0),
            'actual_rps_including_tail': count / total_seconds if total_seconds else 0,
            'elapsed_seconds_including_tail': total_seconds,
            'threshold_pass': bool(count and p95 is not None and p95 < 500 and errors is not None and errors < .01)}
