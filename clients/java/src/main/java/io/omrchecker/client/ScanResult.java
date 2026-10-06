package io.omrchecker.client;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Typed view over one page result; {@link #raw()} has fields, zones, bubbles, timings. */
public final class ScanResult {
    private final Map<String, Object> raw;

    public ScanResult(Map<String, Object> raw) { this.raw = raw; }

    public String scanId() { return (String) raw.get("scan_id"); }
    public String fileName() { return (String) raw.get("file_name"); }
    /** ok | needs_review | error */
    public String status() { return (String) raw.get("status"); }
    public Double score() { Object v = raw.get("score"); return v instanceof Number ? ((Number) v).doubleValue() : null; }
    public String error() { return (String) raw.get("error"); }

    /** {label: value}; "" = blank, "AB" = multi-mark. */
    public Map<String, String> responses() {
        Map<String, String> out = new LinkedHashMap<>();
        Object r = raw.get("responses");
        if (r instanceof Map) {
            for (Map.Entry<?, ?> e : ((Map<?, ?>) r).entrySet()) {
                out.put(String.valueOf(e.getKey()), e.getValue() == null ? null : String.valueOf(e.getValue()));
            }
        }
        return out;
    }

    /** Names of fields/zones a human should check. */
    public List<String> reviewNames() {
        List<String> out = new ArrayList<>();
        Object r = raw.get("review");
        if (r instanceof List) {
            for (Object item : (List<?>) r) out.add(String.valueOf(((Map<?, ?>) item).get("name")));
        }
        return out;
    }

    public Map<String, Object> raw() { return raw; }

    @SuppressWarnings("unchecked")
    public static List<ScanResult> listFrom(String scansResponseJson) {
        Map<String, Object> body = Json.parseObject(scansResponseJson);
        List<ScanResult> out = new ArrayList<>();
        for (Object o : (List<Object>) body.get("scans")) out.add(new ScanResult((Map<String, Object>) o));
        return out;
    }
}
