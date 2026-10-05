package io.omrchecker.client;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON reader/writer so the client needs no dependencies.
 * Objects become {@code Map<String,Object>} (insertion ordered), arrays {@code List<Object>},
 * numbers {@code Double}, plus String, Boolean and null. Swap in Jackson/Gson on the raw
 * strings returned by {@link OmrClient} if you prefer typed binding.
 */
public final class Json {
    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        Json p = new Json(text);
        p.ws();
        Object value = p.value();
        p.ws();
        if (p.i != p.s.length()) throw p.error("trailing characters");
        return value;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        return (Map<String, Object>) parse(text);
    }

    private IllegalArgumentException error(String msg) {
        return new IllegalArgumentException("Invalid JSON at " + i + ": " + msg);
    }

    private void ws() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++;
    }

    private Object value() {
        if (i >= s.length()) throw error("unexpected end");
        char c = s.charAt(i);
        switch (c) {
            case '{': return object();
            case '[': return array();
            case '"': return string();
            case 't': return literal("true", Boolean.TRUE);
            case 'f': return literal("false", Boolean.FALSE);
            case 'n': return literal("null", null);
            default: return number();
        }
    }

    private Object literal(String word, Object value) {
        if (!s.startsWith(word, i)) throw error("expected " + word);
        i += word.length();
        return value;
    }

    private Map<String, Object> object() {
        Map<String, Object> map = new LinkedHashMap<>();
        i++;
        ws();
        if (s.charAt(i) == '}') { i++; return map; }
        while (true) {
            ws();
            if (s.charAt(i) != '"') throw error("expected key");
            String key = string();
            ws();
            if (s.charAt(i++) != ':') throw error("expected ':'");
            ws();
            map.put(key, value());
            ws();
            char c = s.charAt(i++);
            if (c == '}') return map;
            if (c != ',') throw error("expected ',' or '}'");
        }
    }

    private List<Object> array() {
        List<Object> list = new ArrayList<>();
        i++;
        ws();
        if (s.charAt(i) == ']') { i++; return list; }
        while (true) {
            ws();
            list.add(value());
            ws();
            char c = s.charAt(i++);
            if (c == ']') return list;
            if (c != ',') throw error("expected ',' or ']'");
        }
    }

    private String string() {
        StringBuilder out = new StringBuilder();
        i++;
        while (true) {
            if (i >= s.length()) throw error("unterminated string");
            char c = s.charAt(i++);
            if (c == '"') return out.toString();
            if (c != '\\') { out.append(c); continue; }
            char e = s.charAt(i++);
            switch (e) {
                case 'n': out.append('\n'); break;
                case 't': out.append('\t'); break;
                case 'r': out.append('\r'); break;
                case 'b': out.append('\b'); break;
                case 'f': out.append('\f'); break;
                case 'u': out.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
                default: out.append(e);
            }
        }
    }

    private Double number() {
        int start = i;
        while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
        if (start == i) throw error("unexpected character '" + s.charAt(i) + "'");
        return Double.valueOf(s.substring(start, i));
    }

    /** Serialize Maps, Iterables, arrays of Strings, Strings, Numbers, Booleans and null. */
    public static String write(Object value) {
        StringBuilder out = new StringBuilder();
        write(value, out);
        return out.toString();
    }

    private static void write(Object v, StringBuilder out) {
        if (v == null) {
            out.append("null");
        } else if (v instanceof String) {
            quote((String) v, out);
        } else if (v instanceof Number || v instanceof Boolean) {
            out.append(v);
        } else if (v instanceof Map) {
            out.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) out.append(',');
                first = false;
                quote(String.valueOf(e.getKey()), out);
                out.append(':');
                write(e.getValue(), out);
            }
            out.append('}');
        } else if (v instanceof Iterable) {
            out.append('[');
            boolean first = true;
            for (Object item : (Iterable<?>) v) {
                if (!first) out.append(',');
                first = false;
                write(item, out);
            }
            out.append(']');
        } else if (v instanceof Object[]) {
            write(java.util.Arrays.asList((Object[]) v), out);
        } else {
            quote(v.toString(), out);
        }
    }

    private static void quote(String s, StringBuilder out) {
        out.append('"');
        for (int k = 0; k < s.length(); k++) {
            char c = s.charAt(k);
            switch (c) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (c < 0x20) out.append(String.format("\\u%04x", (int) c));
                    else out.append(c);
            }
        }
        out.append('"');
    }
}
