package omrclient

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Offline checks of the transport against a fake server.
func TestMultipartAndErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-Key") != "k" {
			w.WriteHeader(401)
			w.Write([]byte(`{"detail":"Missing or invalid API key"}`))
			return
		}
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Errorf("multipart: %v", err)
		}
		if r.FormValue("template_id") != "t1" || r.FormValue("start") != "false" {
			t.Errorf("fields: %v", r.MultipartForm.Value)
		}
		fh := r.MultipartForm.File["files"]
		if len(fh) != 2 || fh[0].Filename != "a.png" || fh[0].Header.Get("Content-Type") != "image/png" {
			t.Errorf("files: %+v", fh)
		}
		w.Write([]byte(`{"id":"j1","state":"uploading","total_files":2,"counts":{}}`))
	}))
	defer srv.Close()
	ctx := context.Background()

	c := New(srv.URL, "k")
	job, err := c.CreateJob(ctx, JobOptions{TemplateID: "t1", NoStart: true, Files: []File{
		{Name: "a.png", Content: []byte("x")}, {Name: "b.jpg", Content: []byte("y")},
	}})
	if err != nil || job.ID != "j1" || job.TotalFiles != 2 || job.Done() {
		t.Fatalf("job=%+v err=%v", job, err)
	}
	_, err = New(srv.URL, "").Health(ctx)
	apiErr, ok := err.(*APIError)
	if !ok || apiErr.Status != 401 || !strings.Contains(apiErr.Detail, "API key") {
		t.Fatalf("expected 401 APIError, got %v", err)
	}
}

// Integration test against a running server; pytest (src/tests/test_clients.py) sets:
//
//	OMR_API_URL        server base URL
//	OMR_TEST_TEMPLATE  path to template.json
//	OMR_TEST_SHEETS    os.PathListSeparator-separated sheet images (>= 3)
//	OMR_TEST_EXPECT    JSON {label: value} for the first sheet
func TestAgainstServer(t *testing.T) {
	base := os.Getenv("OMR_API_URL")
	if base == "" {
		t.Skip("OMR_API_URL not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	c := New(base, os.Getenv("OMR_API_KEY"))
	sheets := filepath.SplitList(os.Getenv("OMR_TEST_SHEETS"))
	var expect map[string]string
	if err := json.Unmarshal([]byte(os.Getenv("OMR_TEST_EXPECT")), &expect); err != nil {
		t.Fatal(err)
	}

	if _, err := c.Health(ctx); err != nil {
		t.Fatal(err)
	}
	caps, err := c.Capabilities(ctx)
	if err != nil || caps["barcode_formats"] == nil {
		t.Fatalf("capabilities: %v %v", caps, err)
	}
	tpl, err := c.UploadTemplate(ctx, "go-client", []string{os.Getenv("OMR_TEST_TEMPLATE")})
	if err != nil {
		t.Fatal(err)
	}
	templates, err := c.ListTemplates(ctx)
	if err != nil || len(templates) == 0 {
		t.Fatalf("templates: %v %v", templates, err)
	}

	first, _ := FilesFromPaths(sheets[0])
	scans, err := c.Scan(ctx, tpl.ID, first)
	if err != nil || len(scans.Scans) != 1 {
		t.Fatalf("scan: %+v %v", scans, err)
	}
	scan := scans.Scans[0]
	for label, want := range expect {
		if got := scan.Responses[label]; got != want {
			t.Errorf("%s: got %q want %q", label, got, want)
		}
	}
	if f, ok := scan.Fields["q1"]; !ok || len(f.Bubbles) == 0 {
		t.Errorf("typed field q1 missing: %+v", scan.Fields["q1"])
	}
	again, err := c.GetScan(ctx, scan.ScanID)
	if err != nil || again.ScanID != scan.ScanID {
		t.Fatalf("get scan: %v", err)
	}

	// Job built from uploads in two steps
	files, _ := FilesFromPaths(sheets[:3]...)
	job, err := c.CreateJob(ctx, JobOptions{TemplateID: tpl.ID, NoStart: true, Files: files[:1], Workers: 2})
	if err != nil || job.State != "uploading" {
		t.Fatalf("create job: %+v %v", job, err)
	}
	if job, err = c.UploadJobFiles(ctx, job.ID, files[1:]); err != nil || job.TotalFiles != 3 {
		t.Fatalf("upload: %+v %v", job, err)
	}
	if _, err = c.StartJob(ctx, job.ID); err != nil {
		t.Fatal(err)
	}
	polls := 0
	job, err = c.WaitForJob(ctx, job.ID, 200*time.Millisecond, func(*Job) { polls++ })
	if err != nil || job.State != "completed" || job.ProcessedFiles != 3 || polls == 0 {
		t.Fatalf("wait: %+v %v", job, err)
	}
	var buf bytes.Buffer
	if err := c.JobResultsCSVTo(ctx, job.ID, &buf); err != nil {
		t.Fatal(err)
	}
	rows, err := csv.NewReader(io.Reader(&buf)).ReadAll()
	if err != nil || len(rows) != 4 {
		t.Fatalf("csv rows=%d err=%v", len(rows), err)
	}
	col := map[string]int{}
	for i, h := range rows[0] {
		col[h] = i
	}
	for label, want := range expect {
		if got := rows[1][col[label]]; got != want {
			t.Errorf("csv %s: got %q want %q", label, got, want)
		}
	}

	if _, err := c.ReviewQueue(ctx, ReviewQuery{JobID: job.ID, Limit: 10}); err != nil {
		t.Fatal(err)
	}
	reviewed, err := c.SubmitReview(ctx, scan.ScanID, map[string]string{"q1": expect["q1"]}, nil, "go-test")
	if err != nil || !reviewed.Reviewed {
		t.Fatalf("review: %+v %v", reviewed, err)
	}

	// Results screen and exports
	sheet, err := c.Render(ctx, scan.ScanID)
	if err != nil || sheet["image_url"] == nil {
		t.Fatalf("render: %v %v", sheet, err)
	}
	if _, err := c.Correct(ctx, scan.ScanID, nil, []Toggle{{Field: "q2", Value: "A"}}); err != nil {
		t.Fatalf("correct: %v", err)
	}
	if _, err := c.Verify(ctx, scan.ScanID); err != nil {
		t.Fatalf("verify: %v", err)
	}
	if acc, err := c.Accuracy(ctx, tpl.ID, ""); err != nil || acc["verified_sheets"].(float64) < 1 {
		t.Fatalf("accuracy: %v %v", acc, err)
	}
	page, err := c.ListResults(ctx, ResultsQuery{TemplateID: tpl.ID, View: "verified"})
	if err != nil || page.Total != 1 || page.Items[0].ID != scan.ScanID {
		t.Fatalf("results: %+v %v", page, err)
	}
	export, err := c.CreateExport(ctx, ExportRequest{Format: "csv", Filters: map[string]interface{}{"job_id": job.ID}})
	if err != nil {
		t.Fatal(err)
	}
	if export, err = c.WaitForExport(ctx, export.ID, 200*time.Millisecond); err != nil || export.Rows != 3 {
		t.Fatalf("export: %+v %v", export, err)
	}
	out := filepath.Join(t.TempDir(), "export.csv")
	if err := c.DownloadExport(ctx, export.ID, out); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(out); !strings.Contains(string(data), "sheet") {
		t.Fatalf("export content: %q", data)
	}

	if _, err := c.JobStatus(ctx, "doesnotexist"); err == nil {
		t.Fatal("expected 404")
	} else if e, ok := err.(*APIError); !ok || e.Status != 404 {
		t.Fatalf("expected 404 APIError, got %v", err)
	}
}
