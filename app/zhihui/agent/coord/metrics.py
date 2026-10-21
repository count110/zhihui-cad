"""Explicit rate denominators and linearly interpolated percentiles."""
def percentile(values,p):
    values=sorted(values)
    if not values:return None
    i=(len(values)-1)*p;lo=int(i);hi=min(lo+1,len(values)-1)
    return round(values[lo]+(values[hi]-values[lo])*(i-lo),3)


def summarize(results):
    n=len(results)
    usage=[r.get('usage') or {} for r in results]
    calls=[u['tool_calls'] for u in usage if 'tool_calls' in u]
    secs=[r['seconds'] for r in results if 'seconds' in r]
    coverage=[r['coverage'] for r in results if r.get('coverage') is not None]
    return {'tasks':n,'ready_rate':sum(r['status']=='READY' for r in results)/n if n else 0,
        'warning_delivery_rate':sum(r['status']=='READY_WITH_WARNINGS' for r in results)/n if n else 0,
        'failed_rate':sum(r['status'] in {'FAILED','ERROR'} for r in results)/n if n else 0,
        'waiting_confirmation':sum(r['status']=='WAITING_CONFIRMATION' for r in results),
        'tool_calls_mean':round(sum(calls)/len(calls),3) if calls else None,'tool_calls_p95':percentile(calls,.95),
        'seconds_p50':percentile(secs,.5),'seconds_p95':percentile(secs,.95),
        'total_tokens':sum(u.get('prompt_tokens',0)+u.get('completion_tokens',0) for u in usage),
        'repeated_calls_blocked':sum(u.get('repeated_calls_blocked',0) for u in usage),
        'oscillations':sum(u.get('oscillations',0) for u in usage),
        'knowledge_gaps':sum(u.get('knowledge_gap_count',0) for u in usage),
        'confirmations':sum(u.get('confirmation_count',int(r['status']=='WAITING_CONFIRMATION')) for r,u in zip(results,usage)),
        'checklist_coverage_mean':sum(coverage)/len(coverage) if coverage else None,'coverage_samples':len(coverage)}
