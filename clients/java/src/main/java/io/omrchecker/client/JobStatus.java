package io.omrchecker.client;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Typed view over GET /jobs/{id}; {@link #raw()} keeps the full JSON. */
public final class JobStatus {
    private final Map<String, Object> raw;
    private final String json;

    public JobStatus(String json) {
        this.json = json;
        this.raw = Json.parseObject(json);
    }

    public String id() { return str("id"); }
    public String templateId() { return str("template_id"); }
    public String state() { return str("state"); }
    public int totalFiles() { return num("total_files"); }
    public int processedFiles() { return num("processed_files"); }
    public int pages() { return num("pages"); }
    public int pendingReview() { return num("pending_review"); }
    public double progress() { return dbl("progress"); }
    /** Sheets per second while running (null before the first results). */
    public Double throughputPerSecond() { return (Double) raw.get("throughput_per_s"); }
    public Double etaSeconds() { return (Double) raw.get("eta_s"); }

    /** Pages per status: ok, needs_review, error. */
    public Map<String, Integer> counts() {
        Map<String, Integer> out = new LinkedHashMap<>();
        Object c = raw.get("counts");
        if (c instanceof Map) {
            for (Map.Entry<?, ?> e : ((Map<?, ?>) c).entrySet()) {
                out.put(String.valueOf(e.getKey()), ((Number) e.getValue()).intValue());
            }
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    public List<String> errors() {
        Object e = raw.get("errors");
        return e instanceof List ? (List<String>) e : Collections.emptyList();
    }

    public boolean isDone() {
        String s = state();
        return "completed".equals(s) || "failed".equals(s) || "cancelled".equals(s)
                || "interrupted".equals(s);
    }

    public Map<String, Object> raw() { return raw; }
    public String json() { return json; }

    private String str(String k) { Object v = raw.get(k); return v == null ? null : v.toString(); }
    private int num(String k) { Object v = raw.get(k); return v instanceof Number ? ((Number) v).intValue() : 0; }
    private double dbl(String k) { Object v = raw.get(k); return v instanceof Number ? ((Number) v).doubleValue() : 0; }

    @Override
    public String toString() {
        return "JobStatus{" + id() + " " + state() + " " + processedFiles() + "/" + totalFiles()
                + " " + counts() + "}";
    }
}
