package io.omrchecker.client.examples;

import io.omrchecker.client.JobStatus;
import io.omrchecker.client.OmrClient;

import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Read a folder on the server's disk as a bulk job and save the CSV.
 *
 * <pre>
 * java -jar omr-client-1.0.0.jar --url http://127.0.0.1:8000 --template exam \
 *      --folder D:\scans\day1 --out day1.csv [--workers 8] [--api-key KEY]
 * </pre>
 * --template is a template id on the server, or a template.json / .zip path to upload first.
 */
public final class BulkExample {
    public static void main(String[] argv) throws Exception {
        Map<String, String> args = new HashMap<>();
        for (int i = 0; i + 1 < argv.length; i += 2) args.put(argv[i].replaceFirst("^--", ""), argv[i + 1]);
        if (!args.containsKey("template") || !args.containsKey("folder")) {
            System.err.println("usage: --template ID|template.json --folder DIR [--url URL] [--out results.csv] [--workers N] [--api-key KEY]");
            System.exit(2);
        }
        OmrClient client = new OmrClient(args.getOrDefault("url", "http://127.0.0.1:8000"),
                args.getOrDefault("api-key", System.getenv("OMR_API_KEY")));

        String templateId = args.get("template");
        String lower = templateId.toLowerCase();
        if (lower.endsWith(".json") || lower.endsWith(".zip")) {
            templateId = client.uploadTemplateId(null, List.of(Path.of(templateId)));
            System.out.println("uploaded template " + templateId);
        }
        Instant start = Instant.now();
        int workers = Integer.parseInt(args.getOrDefault("workers", "0"));
        JobStatus job = client.createFolderJob(templateId, args.get("folder"), workers);
        System.out.println("job " + job.id() + ": " + job.totalFiles() + " files");
        job = client.waitForJob(job.id(), Duration.ofSeconds(2), j -> {
            Double rate = j.throughputPerSecond();
            System.out.printf("\r%-10s %d/%d (%.0f/min) %s", j.state(), j.processedFiles(), j.totalFiles(),
                    rate == null ? 0.0 : rate * 60, j.counts());
        });
        System.out.println();
        Path out = Path.of(args.getOrDefault("out", "results.csv"));
        client.jobResultsCsv(job.id(), out);
        double seconds = Duration.between(start, Instant.now()).toMillis() / 1000.0;
        System.out.printf("%s: %d files in %.1fs (%.0f sheets/min), %d review items, CSV -> %s%n",
                job.state(), job.processedFiles(), seconds, job.processedFiles() / seconds * 60,
                job.pendingReview(), out);
        for (String e : job.errors()) System.out.println("  error: " + e);
        System.exit("completed".equals(job.state()) ? 0 : 1);
    }
}
