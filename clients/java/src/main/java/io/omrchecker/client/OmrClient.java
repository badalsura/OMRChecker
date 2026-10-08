package io.omrchecker.client;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Consumer;

/**
 * Zero-dependency Java 11+ client for the OMR Engine REST API.
 *
 * <pre>
 * OmrClient client = new OmrClient("http://127.0.0.1:8000", null);
 * String templateId = client.uploadTemplateId("exam", List.of(Path.of("exam/template.json")));
 * JobStatus job = client.createFolderJob(templateId, "D:\\scans\\day1", 8);
 * job = client.waitForJob(job.id(), Duration.ofSeconds(2), j -> System.out.println(j));
 * client.jobResultsCsv(job.id(), Path.of("day1.csv"));
 * </pre>
 *
 * Methods return the server's raw JSON string, or a typed view ({@link JobStatus},
 * {@link ScanResult}) where it helps. Non-2xx responses throw {@link OmrApiException}.
 * Instances are thread safe.
 */
public class OmrClient {
    private final String baseUrl;
    private final String apiKey;
    private final HttpClient http;
    private Duration requestTimeout = Duration.ofMinutes(5);

    public OmrClient(String baseUrl, String apiKey) {
        this(baseUrl, apiKey, HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(30))
                .build());
    }

    public OmrClient(String baseUrl, String apiKey, HttpClient http) {
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.apiKey = apiKey;
        this.http = http;
    }

    public OmrClient withRequestTimeout(Duration timeout) {
        this.requestTimeout = timeout;
        return this;
    }

    /** An in-memory upload. */
    public static final class Upload {
        final String name;
        final byte[] content;
        final String contentType;

        public Upload(String name, byte[] content, String contentType) {
            this.name = name;
            this.content = content;
            this.contentType = contentType != null ? contentType : guessType(name);
        }

        public static Upload of(Path path) throws IOException {
            return new Upload(path.getFileName().toString(), Files.readAllBytes(path), null);
        }
    }

    // ------------------------------------------------------------ meta

    public String health() throws IOException, InterruptedException {
        return get("/health", null);
    }

    public String capabilities() throws IOException, InterruptedException {
        return get("/capabilities", null);
    }

    // ------------------------------------------------------------ templates

    /** {"templates": [...]} */
    public String listTemplates() throws IOException, InterruptedException {
        return get("/templates", null);
    }

    public String getTemplate(String templateId) throws IOException, InterruptedException {
        return get("/templates/" + enc(templateId), null);
    }

    /** Upload template.json (+ config.json, evaluation.json, marker images) or one .zip. */
    public String uploadTemplate(String name, Collection<Path> paths) throws IOException, InterruptedException {
        Map<String, String> fields = new LinkedHashMap<>();
        if (name != null) fields.put("name", name);
        return multipart("/templates", fields, uploads(paths));
    }

    /** Same as {@link #uploadTemplate} but returns only the new template id. */
    public String uploadTemplateId(String name, Collection<Path> paths) throws IOException, InterruptedException {
        return (String) Json.parseObject(uploadTemplate(name, paths)).get("id");
    }

    // ------------------------------------------------------------ scans

    /** Read a few sheets synchronously: {"scans": [result, ...]}. */
    public String scan(String templateId, Collection<Path> files) throws IOException, InterruptedException {
        return scanUploads(templateId, uploads(files));
    }

    public String scanUploads(String templateId, List<Upload> files) throws IOException, InterruptedException {
        Map<String, String> fields = new LinkedHashMap<>();
        fields.put("template_id", templateId);
        return multipart("/scans", fields, files);
    }

    /** Typed variant of {@link #scan}. */
    public List<ScanResult> scanResults(String templateId, Collection<Path> files) throws IOException, InterruptedException {
        return ScanResult.listFrom(scan(templateId, files));
    }

    public String getScan(String scanId) throws IOException, InterruptedException {
        return get("/scans/" + enc(scanId), null);
    }

    /** filters: status, template_id, job_id, reviewed, limit, offset. */
    public String listScans(Map<String, String> filters) throws IOException, InterruptedException {
        return get("/scans", filters);
    }

    // ------------------------------------------------------------ jobs

    /**
     * Create a bulk job. files are uploaded; folder (nullable) is read on the server's own disk.
     * workers &lt;= 0 uses the server default. start=false lets you add files with
     * {@link #uploadJobFiles} and then call {@link #startJob}.
     */
    public JobStatus createJob(String templateId, Collection<Path> files, String folder, int workers, boolean start)
            throws IOException, InterruptedException {
        Map<String, String> fields = new LinkedHashMap<>();
        fields.put("template_id", templateId);
        fields.put("start", Boolean.toString(start));
        if (folder != null) fields.put("folder", folder);
        if (workers > 0) fields.put("workers", Integer.toString(workers));
        return new JobStatus(multipart("/jobs", fields, uploads(files)));
    }

    public JobStatus createFolderJob(String templateId, String folder, int workers) throws IOException, InterruptedException {
        return createJob(templateId, null, folder, workers, true);
    }

    public JobStatus uploadJobFiles(String jobId, Collection<Path> files) throws IOException, InterruptedException {
        return new JobStatus(multipart("/jobs/" + enc(jobId) + "/files", Map.of(), uploads(files)));
    }

    public JobStatus startJob(String jobId) throws IOException, InterruptedException {
        return new JobStatus(send("POST", "/jobs/" + enc(jobId) + "/start", null, HttpRequest.BodyPublishers.noBody(), null));
    }

    public JobStatus cancelJob(String jobId) throws IOException, InterruptedException {
        return new JobStatus(send("POST", "/jobs/" + enc(jobId) + "/cancel", null, HttpRequest.BodyPublishers.noBody(), null));
    }

    public JobStatus jobStatus(String jobId) throws IOException, InterruptedException {
        return new JobStatus(get("/jobs/" + enc(jobId), null));
    }

    /** Poll until the job is final; onPoll (nullable) sees every status. */
    public JobStatus waitForJob(String jobId, Duration poll, Consumer<JobStatus> onPoll)
            throws IOException, InterruptedException {
        while (true) {
            JobStatus job = jobStatus(jobId);
            if (onPoll != null) onPoll.accept(job);
            if (job.isDone()) return job;
            Thread.sleep(poll.toMillis());
        }
    }

    /** Stream the job's CSV (one row per page) to outPath. */
    public Path jobResultsCsv(String jobId, Path outPath) throws IOException, InterruptedException {
        HttpResponse<InputStream> response = http.send(
                request("/jobs/" + enc(jobId) + "/results.csv", null).GET().build(),
                HttpResponse.BodyHandlers.ofInputStream());
        try (InputStream body = response.body()) {
            if (response.statusCode() >= 300) {
                String text = new String(body.readAllBytes(), StandardCharsets.UTF_8);
                throw apiError(response.statusCode(), text);
            }
            Files.copy(body, outPath, StandardCopyOption.REPLACE_EXISTING);
        }
        return outPath;
    }

    // ------------------------------------------------------------ review

    /** Pending human-review items. params: template_id, job_id, scan_id, name, kind, limit, offset. */
    public String reviewQueue(Map<String, String> params) throws IOException, InterruptedException {
        return get("/review", params);
    }

    /** corrections: {name: value}; accept: names whose read value is right. */
    public String submitReview(String scanId, Map<String, String> corrections, Collection<String> accept, String reviewer)
            throws IOException, InterruptedException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("corrections", corrections != null ? corrections : Map.of());
        body.put("accept", accept != null ? accept : List.of());
        if (reviewer != null) body.put("reviewer", reviewer);
        return send("POST", "/scans/" + enc(scanId) + "/review", null,
                HttpRequest.BodyPublishers.ofString(Json.write(body)), "application/json");
    }

    /** Pending total and per-name counts; params: template_id, job_id, scan_id, name, kind, since. */
    public String reviewCounts(Map<String, String> params) throws IOException, InterruptedException {
        return get("/review/counts", params);
    }

    /** Accept pending items under the filters as read (who and when are recorded; nothing is deleted).
     *  filters: template_id, job_id, scan_id, name, kind, expected, before, limit. */
    public String acceptReviewBulk(Map<String, Object> filters) throws IOException, InterruptedException {
        return postJson("/review/accept-bulk", filters != null ? filters : Map.of());
    }

    // ------------------------------------------------------------ housekeeping

    /** Delete one sheet's result and stored images (audited). */
    public String deleteScan(String scanId) throws IOException, InterruptedException {
        return send("DELETE", "/results/" + enc(scanId), null, null, null);
    }

    /** Delete a finished job and its results (audited); server-folder files are never touched. */
    public String deleteJob(String jobId) throws IOException, InterruptedException {
        return send("DELETE", "/jobs/" + enc(jobId), null, null, null);
    }

    /** Is this server folder usable? {ok, images, pdfs} or {ok: false, error}. */
    public String checkFolder(String path, boolean recursive) throws IOException, InterruptedException {
        return get("/fs/check", Map.of("path", path, "recursive", String.valueOf(recursive)));
    }

    // ------------------------------------------------------------ results

    /** Graded sheets. params: template_id, job_id, status, view (all, flagged, reviewed, not_reviewed,
     *  verified, corrected, errors), name, flag, file, order, limit, offset. */
    public String listResults(Map<String, String> params) throws IOException, InterruptedException {
        return get("/results", params);
    }

    /** Re-render a sheet: overlay geometry (bubbles, zones, values) plus image_url. */
    public String render(String scanId) throws IOException, InterruptedException {
        return get("/scans/" + enc(scanId) + "/render", null);
    }

    /** Change values ({name: value}) and/or toggle bubbles (each {field, value}); recomputed server-side. */
    public String correct(String scanId, Map<String, String> changes, List<Map<String, String>> toggles)
            throws IOException, InterruptedException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("changes", changes != null ? changes : Map.of());
        body.put("toggle", toggles != null ? toggles : List.of());
        return postJson("/scans/" + enc(scanId) + "/corrections", body);
    }

    /** Every value of the sheet was checked by a person (feeds the accuracy readout). */
    public String verify(String scanId) throws IOException, InterruptedException {
        return postJson("/scans/" + enc(scanId) + "/verify", Map.of());
    }

    /** Re-read with template/config overrides; apply=false returns a preview. */
    public String regrade(String scanId, Map<String, Object> templateOverrides, Map<String, Object> configOverrides,
                          boolean apply) throws IOException, InterruptedException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("template_overrides", templateOverrides != null ? templateOverrides : Map.of());
        body.put("config_overrides", configOverrides != null ? configOverrides : Map.of());
        body.put("apply", apply);
        return postJson("/scans/" + enc(scanId) + "/regrade", body);
    }

    /** params: template_id, job_id. */
    public String accuracy(Map<String, String> params) throws IOException, InterruptedException {
        return get("/results/accuracy", params);
    }

    // ------------------------------------------------------------ exports

    /** format: csv, xlsx, pdf, sqlite or sql; filters: job_id, template_id, view, ...; profile may be null. */
    public String createExport(String format, Map<String, Object> filters, Map<String, Object> profile, boolean waitForIt)
            throws IOException, InterruptedException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("format", format);
        body.put("filters", filters != null ? filters : Map.of());
        if (profile != null) body.put("profile", profile);
        body.put("wait", waitForIt);
        return postJson("/exports", body);
    }

    public String exportStatus(String exportId) throws IOException, InterruptedException {
        return get("/exports/" + enc(exportId), null);
    }

    /** Save a completed export to outPath. */
    public Path downloadExport(String exportId, Path outPath) throws IOException, InterruptedException {
        HttpResponse<InputStream> response = http.send(
                request("/exports/" + enc(exportId) + "/download", null).GET().build(),
                HttpResponse.BodyHandlers.ofInputStream());
        try (InputStream body = response.body()) {
            if (response.statusCode() >= 300) {
                String text = new String(body.readAllBytes(), StandardCharsets.UTF_8);
                throw apiError(response.statusCode(), text);
            }
            Files.copy(body, outPath, StandardCopyOption.REPLACE_EXISTING);
        }
        return outPath;
    }

    // ------------------------------------------------------------ transport

    private String postJson(String path, Object body) throws IOException, InterruptedException {
        return send("POST", path, null, HttpRequest.BodyPublishers.ofString(Json.write(body)), "application/json");
    }

    private String get(String path, Map<String, String> params) throws IOException, InterruptedException {
        return send("GET", path, params, null, null);
    }

    private HttpRequest.Builder request(String path, Map<String, String> params) {
        StringBuilder url = new StringBuilder(baseUrl).append(path);
        if (params != null && !params.isEmpty()) {
            char sep = '?';
            for (Map.Entry<String, String> e : params.entrySet()) {
                if (e.getValue() == null) continue;
                url.append(sep).append(enc(e.getKey())).append('=').append(enc(e.getValue()));
                sep = '&';
            }
        }
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(url.toString()))
                .timeout(requestTimeout)
                .header("Accept", "application/json");
        if (apiKey != null && !apiKey.isEmpty()) b.header("X-API-Key", apiKey);
        return b;
    }

    private String send(String method, String path, Map<String, String> params,
                        HttpRequest.BodyPublisher body, String contentType) throws IOException, InterruptedException {
        HttpRequest.Builder b = request(path, params);
        if (contentType != null) b.header("Content-Type", contentType);
        b.method(method, body != null ? body : HttpRequest.BodyPublishers.noBody());
        HttpResponse<String> response = http.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (response.statusCode() >= 300) throw apiError(response.statusCode(), response.body());
        return response.body();
    }

    private String multipart(String path, Map<String, String> fields, List<Upload> files)
            throws IOException, InterruptedException {
        String boundary = "----omr" + UUID.randomUUID().toString().replace("-", "");
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        for (Map.Entry<String, String> f : fields.entrySet()) {
            if (f.getValue() == null) continue;
            write(out, "--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + f.getKey() + "\"\r\n\r\n"
                    + f.getValue() + "\r\n");
        }
        for (Upload u : files) {
            String name = u.name.replace("\"", "%22").replace("\r", "").replace("\n", "");
            write(out, "--" + boundary + "\r\nContent-Disposition: form-data; name=\"files\"; filename=\"" + name
                    + "\"\r\nContent-Type: " + u.contentType + "\r\n\r\n");
            out.write(u.content);
            write(out, "\r\n");
        }
        write(out, "--" + boundary + "--\r\n");
        return send("POST", path, null, HttpRequest.BodyPublishers.ofByteArray(out.toByteArray()),
                "multipart/form-data; boundary=" + boundary);
    }

    private static void write(ByteArrayOutputStream out, String s) {
        byte[] b = s.getBytes(StandardCharsets.UTF_8);
        out.write(b, 0, b.length);
    }

    private static List<Upload> uploads(Collection<Path> paths) throws IOException {
        List<Upload> out = new ArrayList<>();
        if (paths != null) for (Path p : paths) out.add(Upload.of(p));
        return out;
    }

    private static OmrApiException apiError(int status, String body) {
        String detail = body;
        try {
            Object parsed = Json.parse(body);
            if (parsed instanceof Map && ((Map<?, ?>) parsed).containsKey("detail")) {
                Object d = ((Map<?, ?>) parsed).get("detail");
                detail = d instanceof String ? (String) d : Json.write(d);
            }
        } catch (RuntimeException ignored) {
            // not JSON
        }
        return new OmrApiException(status, detail, body);
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8).replace("+", "%20");
    }

    static String guessType(String name) {
        String n = name.toLowerCase();
        if (n.endsWith(".png")) return "image/png";
        if (n.endsWith(".jpg") || n.endsWith(".jpeg")) return "image/jpeg";
        if (n.endsWith(".tif") || n.endsWith(".tiff")) return "image/tiff";
        if (n.endsWith(".bmp")) return "image/bmp";
        if (n.endsWith(".webp")) return "image/webp";
        if (n.endsWith(".pdf")) return "application/pdf";
        if (n.endsWith(".json")) return "application/json";
        if (n.endsWith(".zip")) return "application/zip";
        if (n.endsWith(".csv")) return "text/csv";
        return "application/octet-stream";
    }
}
