package io.omrchecker.client.examples;

import io.omrchecker.client.Json;
import io.omrchecker.client.JobStatus;
import io.omrchecker.client.OmrApiException;
import io.omrchecker.client.OmrClient;
import io.omrchecker.client.ScanResult;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * End-to-end check used by src/tests/test_clients.py:
 * SmokeCheck URL TEMPLATE_JSON SHEETS(path-separator list, >= 3) EXPECT_JSON OUT_CSV
 */
public final class SmokeCheck {
    static void check(boolean ok, String what) {
        if (!ok) throw new AssertionError(what);
    }

    public static void main(String[] a) throws Exception {
        OmrClient c = new OmrClient(a[0], System.getenv("OMR_API_KEY"));
        List<Path> sheets = new ArrayList<>();
        for (String s : a[2].split(File.pathSeparator)) sheets.add(Path.of(s));
        Map<String, Object> expect = Json.parseObject(a[3]);

        check("ok".equals(Json.parseObject(c.health()).get("status")), "health");
        check(c.capabilities().contains("barcode_formats"), "capabilities");
        String templateId = c.uploadTemplateId("java-client", List.of(Path.of(a[1])));
        check(c.listTemplates().contains(templateId), "listTemplates");

        ScanResult scan = c.scanResults(templateId, sheets.subList(0, 1)).get(0);
        for (Map.Entry<String, Object> e : expect.entrySet()) {
            check(e.getValue().equals(scan.responses().get(e.getKey())),
                    e.getKey() + ": got " + scan.responses().get(e.getKey()) + " want " + e.getValue());
        }
        check(c.getScan(scan.scanId()).contains(scan.scanId()), "getScan");

        JobStatus job = c.createJob(templateId, sheets.subList(0, 1), null, 2, false);
        check("uploading".equals(job.state()), "job state " + job.state());
        job = c.uploadJobFiles(job.id(), sheets.subList(1, 3));
        check(job.totalFiles() == 3, "total files " + job.totalFiles());
        c.startJob(job.id());
        job = c.waitForJob(job.id(), Duration.ofMillis(200), null);
        check("completed".equals(job.state()) && job.processedFiles() == 3, "job " + job);

        Path csv = c.jobResultsCsv(job.id(), Path.of(a[4]));
        List<String> lines = Files.readAllLines(csv);
        check(lines.size() == 4, "csv lines " + lines.size());

        c.reviewQueue(Map.of("job_id", job.id(), "limit", "10"));
        String reviewed = c.submitReview(scan.scanId(), Map.of("q1", (String) expect.get("q1")), List.of(), "java");
        check(Boolean.TRUE.equals(Json.parseObject(reviewed).get("reviewed")), "review");

        // Results screen and exports
        check(Json.parseObject(c.render(scan.scanId())).get("image_url") != null, "render");
        c.correct(scan.scanId(), null, List.of(Map.of("field", "q2", "value", "A")));
        check(Json.parseObject(c.verify(scan.scanId())).get("verified") != null, "verify");
        check(c.listResults(Map.of("view", "verified", "template_id", templateId)).contains(scan.scanId()), "results");
        check(c.accuracy(Map.of("template_id", templateId)).contains("auto_accuracy"), "accuracy");
        Map<String, Object> export = Json.parseObject(
                c.createExport("csv", Map.of("job_id", job.id()), null, true));
        check("completed".equals(export.get("state")), "export " + export);
        Path exported = c.downloadExport((String) export.get("id"), Path.of(a[4] + ".export.csv"));
        check(Files.readAllLines(exported).size() == 4, "export lines");

        try {
            c.jobStatus("doesnotexist");
            check(false, "expected 404");
        } catch (OmrApiException e) {
            check(e.getStatus() == 404, "status " + e.getStatus());
        }
        System.out.println("JAVA CLIENT OK " + job);
    }
}
