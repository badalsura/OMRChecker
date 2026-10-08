// Package omrclient is a standard-library-only client for the OMR Engine REST API.
//
//	c := omrclient.New("http://127.0.0.1:8000", "")
//	tpl, _ := c.UploadTemplate(ctx, "", []string{"exam/template.json"})
//	job, _ := c.CreateJob(ctx, omrclient.JobOptions{TemplateID: tpl.ID, Folder: `D:\scans`})
//	job, _ = c.WaitForJob(ctx, job.ID, 2*time.Second, func(j *omrclient.Job) { log.Println(j.Progress) })
//	_ = c.JobResultsCSV(ctx, job.ID, "results.csv")
package omrclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Final job states: WaitForJob returns once a job reaches one of these.
var FinalJobStates = map[string]bool{
	"completed": true, "failed": true, "cancelled": true, "interrupted": true,
}

// Client talks to one OMR API server. It is safe for concurrent use.
type Client struct {
	BaseURL    string
	APIKey     string
	HTTPClient *http.Client
}

// New returns a client with a 5 minute request timeout.
func New(baseURL, apiKey string) *Client {
	return &Client{
		BaseURL:    strings.TrimRight(baseURL, "/"),
		APIKey:     apiKey,
		HTTPClient: &http.Client{Timeout: 5 * time.Minute},
	}
}

// APIError is returned for non-2xx responses.
type APIError struct {
	Status int
	Detail string
	Body   []byte
}

func (e *APIError) Error() string { return fmt.Sprintf("HTTP %d: %s", e.Status, e.Detail) }

// File is an in-memory upload; use FilesFromPaths for files on disk.
type File struct {
	Name        string
	Content     []byte
	ContentType string
}

// ---------------------------------------------------------------- types

type Template struct {
	ID               string                 `json:"id"`
	Name             string                 `json:"name"`
	Status           string                 `json:"status"`
	FieldBlocks      int                    `json:"field_blocks"`
	Zones            int                    `json:"zones"`
	PageDimensions   []int                  `json:"page_dimensions"`
	HasEvaluation    bool                   `json:"has_evaluation"`
	Files            []string               `json:"files"`
	Template         map[string]interface{} `json:"template,omitempty"`
	ValidationErrors []interface{}          `json:"validation_errors,omitempty"`
}

type Bubble struct {
	Value         string  `json:"value"`
	X             float64 `json:"x"`
	Y             float64 `json:"y"`
	W             float64 `json:"w"`
	H             float64 `json:"h"`
	MeanIntensity float64 `json:"mean_intensity"`
	FillRatio     float64 `json:"fill_ratio"`
	Marked        bool    `json:"marked"`
	Confidence    float64 `json:"confidence"`
}

type FieldResult struct {
	Label       string   `json:"label"`
	Value       string   `json:"value"`
	Confidence  float64  `json:"confidence"`
	Flags       []string `json:"flags"`
	NeedsReview bool     `json:"needs_review"`
	Bubbles     []Bubble `json:"bubbles"`
}

type ZoneResult struct {
	Type        string                 `json:"type"`
	Value       string                 `json:"value"`
	Confidence  float64                `json:"confidence"`
	Flags       []string               `json:"flags"`
	NeedsReview bool                   `json:"needs_review"`
	Details     map[string]interface{} `json:"details,omitempty"`
}

type ReviewFlag struct {
	Kind  string   `json:"kind"`
	Name  string   `json:"name"`
	Flags []string `json:"flags"`
}

// ScanResult is one page read by the engine.
type ScanResult struct {
	ScanID     string                 `json:"scan_id"`
	FileID     string                 `json:"file_id"`
	FileName   string                 `json:"file_name"`
	TemplateID string                 `json:"template_id"`
	JobID      *string                `json:"job_id"`
	Page       int                    `json:"page"`
	Status     string                 `json:"status"` // ok | needs_review | error
	Responses  map[string]string      `json:"responses"`
	Fields     map[string]FieldResult `json:"fields"`
	Zones      map[string]ZoneResult  `json:"zones"`
	Review     []ReviewFlag           `json:"review"`
	Score      *float64               `json:"score"`
	Error      *string                `json:"error"`
	Reviewed   bool                   `json:"reviewed"`
	TimingsMs  map[string]float64     `json:"timings_ms"`
	Links      map[string]*string     `json:"links"`
}

type ScanResponse struct {
	Scans []ScanResult `json:"scans"`
}

// Job is a bulk job's status.
type Job struct {
	ID             string                 `json:"id"`
	TemplateID     string                 `json:"template_id"`
	Name           string                 `json:"name"`
	Source         string                 `json:"source"`
	Folder         string                 `json:"folder,omitempty"`
	State          string                 `json:"state"`
	Options        map[string]interface{} `json:"options"`
	TotalFiles     int                    `json:"total_files"`
	ProcessedFiles int                    `json:"processed_files"`
	Pages          int                    `json:"pages"`
	Counts         map[string]int         `json:"counts"`
	Errors         []string               `json:"errors"`
	CreatedAt      float64                `json:"created_at"`
	StartedAt      *float64               `json:"started_at"`
	FinishedAt     *float64               `json:"finished_at"`
	ThroughputPerS *float64               `json:"throughput_per_s"`
	EtaS           *float64               `json:"eta_s"`
	Progress       float64                `json:"progress"`
	PendingReview  int                    `json:"pending_review"`
}

// Done reports whether the job reached a final state.
func (j *Job) Done() bool { return FinalJobStates[j.State] }

type ReviewOption struct {
	Value      string   `json:"value"`
	Marked     *bool    `json:"marked"`
	FillRatio  *float64 `json:"fill_ratio"`
	Confidence *float64 `json:"confidence"`
}

type ReviewItem struct {
	ScanID     string         `json:"scan_id"`
	FileID     string         `json:"file_id"`
	TemplateID string         `json:"template_id"`
	JobID      *string        `json:"job_id"`
	Kind       string         `json:"kind"`
	Name       string         `json:"name"`
	Type       string         `json:"type"`
	Value      string         `json:"value"`
	Confidence *float64       `json:"confidence"`
	Flags      []string       `json:"flags"`
	CropURL    *string        `json:"crop_url"`
	Options    []ReviewOption `json:"options"`
}

type ReviewQueue struct {
	Items []ReviewItem `json:"items"`
	Total int          `json:"total"`
}

type ReviewQuery struct {
	TemplateID, JobID, ScanID, Name, Kind string
	Limit, Offset                         int
}

// JobOptions configures CreateJob. Files are uploaded; Folder is read on the server.
type JobOptions struct {
	TemplateID string
	Files      []File
	Folder     string
	Workers    int    // 0 = server default
	NoStart    bool   // true: add files with UploadJobFiles, then StartJob
	Recursive  *bool  // default true
	SaveImages string // all | review (default) | none
	Name       string
}

// ---------------------------------------------------------------- transport

func (c *Client) do(ctx context.Context, method, path string, query url.Values, body io.Reader, contentType string) (*http.Response, error) {
	u := c.BaseURL + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if c.APIKey != "" {
		req.Header.Set("X-API-Key", c.APIKey)
	}
	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 {
		defer resp.Body.Close()
		raw, _ := io.ReadAll(resp.Body)
		detail := string(raw)
		var payload map[string]interface{}
		if json.Unmarshal(raw, &payload) == nil {
			if d, ok := payload["detail"]; ok {
				if s, ok := d.(string); ok {
					detail = s
				} else if b, err := json.Marshal(d); err == nil {
					detail = string(b)
				}
			}
		}
		return nil, &APIError{Status: resp.StatusCode, Detail: detail, Body: raw}
	}
	return resp, nil
}

func (c *Client) doJSON(ctx context.Context, method, path string, query url.Values, body io.Reader, contentType string, out interface{}) error {
	resp, err := c.do(ctx, method, path, query, body, contentType)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if out == nil {
		_, err = io.Copy(io.Discard, resp.Body)
		return err
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (c *Client) multipart(ctx context.Context, path string, fields map[string]string, fileField string, files []File, out interface{}) error {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	for k, v := range fields {
		if err := w.WriteField(k, v); err != nil {
			return err
		}
	}
	for _, f := range files {
		h := make(textproto.MIMEHeader)
		h.Set("Content-Disposition", fmt.Sprintf(`form-data; name="%s"; filename="%s"`, fileField, escapeQuotes(f.Name)))
		ct := f.ContentType
		if ct == "" {
			ct = contentTypeFor(f.Name)
		}
		h.Set("Content-Type", ct)
		part, err := w.CreatePart(h)
		if err != nil {
			return err
		}
		if _, err := part.Write(f.Content); err != nil {
			return err
		}
	}
	if err := w.Close(); err != nil {
		return err
	}
	return c.doJSON(ctx, http.MethodPost, path, nil, &buf, w.FormDataContentType(), out)
}

func escapeQuotes(s string) string {
	return strings.NewReplacer(`"`, "%22", "\r", "", "\n", "").Replace(s)
}

func contentTypeFor(name string) string {
	switch strings.ToLower(filepath.Ext(name)) {
	case ".png":
		return "image/png"
	case ".jpg", ".jpeg":
		return "image/jpeg"
	case ".tif", ".tiff":
		return "image/tiff"
	case ".bmp":
		return "image/bmp"
	case ".webp":
		return "image/webp"
	case ".pdf":
		return "application/pdf"
	case ".json":
		return "application/json"
	case ".zip":
		return "application/zip"
	case ".csv":
		return "text/csv"
	}
	return "application/octet-stream"
}

// FilesFromPaths reads files from disk for upload.
func FilesFromPaths(paths ...string) ([]File, error) {
	files := make([]File, 0, len(paths))
	for _, p := range paths {
		content, err := os.ReadFile(p)
		if err != nil {
			return nil, err
		}
		files = append(files, File{Name: filepath.Base(p), Content: content})
	}
	return files, nil
}

// ---------------------------------------------------------------- meta

func (c *Client) Health(ctx context.Context) (map[string]interface{}, error) {
	var out map[string]interface{}
	return out, c.doJSON(ctx, http.MethodGet, "/health", nil, nil, "", &out)
}

func (c *Client) Capabilities(ctx context.Context) (map[string]interface{}, error) {
	var out map[string]interface{}
	return out, c.doJSON(ctx, http.MethodGet, "/capabilities", nil, nil, "", &out)
}

// ---------------------------------------------------------------- templates

func (c *Client) ListTemplates(ctx context.Context) ([]Template, error) {
	var out struct {
		Templates []Template `json:"templates"`
	}
	err := c.doJSON(ctx, http.MethodGet, "/templates", nil, nil, "", &out)
	return out.Templates, err
}

func (c *Client) GetTemplate(ctx context.Context, id string) (*Template, error) {
	var out Template
	return &out, c.doJSON(ctx, http.MethodGet, "/templates/"+url.PathEscape(id), nil, nil, "", &out)
}

// UploadTemplate uploads template.json (+ config.json, evaluation.json, marker images) or one .zip.
func (c *Client) UploadTemplate(ctx context.Context, name string, paths []string) (*Template, error) {
	files, err := FilesFromPaths(paths...)
	if err != nil {
		return nil, err
	}
	return c.UploadTemplateFiles(ctx, name, files)
}

func (c *Client) UploadTemplateFiles(ctx context.Context, name string, files []File) (*Template, error) {
	fields := map[string]string{}
	if name != "" {
		fields["name"] = name
	}
	var out Template
	return &out, c.multipart(ctx, "/templates", fields, "files", files, &out)
}

// DuplicateTemplate copies template, config, answer key and reference image
// under a new unique name. Scans and results stay with the original.
func (c *Client) DuplicateTemplate(ctx context.Context, id, name string) (*Template, error) {
	var out Template
	return &out, c.postJSON(ctx, http.MethodPost, "/templates/"+url.PathEscape(id)+"/duplicate", map[string]string{"name": name}, &out)
}

// RenameTemplate changes the display name (must be unique); the id stays.
func (c *Client) RenameTemplate(ctx context.Context, id, name string) (*Template, error) {
	var out Template
	return &out, c.postJSON(ctx, http.MethodPost, "/templates/"+url.PathEscape(id)+"/rename", map[string]string{"name": name}, &out)
}

func (c *Client) DeleteTemplate(ctx context.Context, id string, purgeScans bool) error {
	q := url.Values{"purge_scans": {strconv.FormatBool(purgeScans)}}
	return c.doJSON(ctx, http.MethodDelete, "/templates/"+url.PathEscape(id), q, nil, "", nil)
}

// ---------------------------------------------------------------- scans

// Scan reads a few sheets synchronously (server limit: sync_max_files per call).
func (c *Client) Scan(ctx context.Context, templateID string, files []File) (*ScanResponse, error) {
	var out ScanResponse
	fields := map[string]string{"template_id": templateID}
	return &out, c.multipart(ctx, "/scans", fields, "files", files, &out)
}

func (c *Client) GetScan(ctx context.Context, scanID string) (*ScanResult, error) {
	var out ScanResult
	return &out, c.doJSON(ctx, http.MethodGet, "/scans/"+url.PathEscape(scanID), nil, nil, "", &out)
}

// ---------------------------------------------------------------- jobs

func (c *Client) CreateJob(ctx context.Context, o JobOptions) (*Job, error) {
	fields := map[string]string{"template_id": o.TemplateID, "start": strconv.FormatBool(!o.NoStart)}
	if o.Folder != "" {
		fields["folder"] = o.Folder
	}
	if o.Workers > 0 {
		fields["workers"] = strconv.Itoa(o.Workers)
	}
	if o.Recursive != nil {
		fields["recursive"] = strconv.FormatBool(*o.Recursive)
	}
	if o.SaveImages != "" {
		fields["save_images"] = o.SaveImages
	}
	if o.Name != "" {
		fields["name"] = o.Name
	}
	var out Job
	return &out, c.multipart(ctx, "/jobs", fields, "files", o.Files, &out)
}

func (c *Client) UploadJobFiles(ctx context.Context, jobID string, files []File) (*Job, error) {
	var out Job
	return &out, c.multipart(ctx, "/jobs/"+url.PathEscape(jobID)+"/files", nil, "files", files, &out)
}

func (c *Client) StartJob(ctx context.Context, jobID string) (*Job, error) {
	var out Job
	return &out, c.doJSON(ctx, http.MethodPost, "/jobs/"+url.PathEscape(jobID)+"/start", nil, nil, "", &out)
}

func (c *Client) CancelJob(ctx context.Context, jobID string) (*Job, error) {
	var out Job
	return &out, c.doJSON(ctx, http.MethodPost, "/jobs/"+url.PathEscape(jobID)+"/cancel", nil, nil, "", &out)
}

func (c *Client) JobStatus(ctx context.Context, jobID string) (*Job, error) {
	var out Job
	return &out, c.doJSON(ctx, http.MethodGet, "/jobs/"+url.PathEscape(jobID), nil, nil, "", &out)
}

// WaitForJob polls until the job is final. onPoll (optional) sees every status.
func (c *Client) WaitForJob(ctx context.Context, jobID string, poll time.Duration, onPoll func(*Job)) (*Job, error) {
	if poll <= 0 {
		poll = 2 * time.Second
	}
	for {
		job, err := c.JobStatus(ctx, jobID)
		if err != nil {
			return nil, err
		}
		if onPoll != nil {
			onPoll(job)
		}
		if job.Done() {
			return job, nil
		}
		select {
		case <-ctx.Done():
			return job, ctx.Err()
		case <-time.After(poll):
		}
	}
}

// JobResultsCSVTo streams the job's CSV (one row per page) into w.
func (c *Client) JobResultsCSVTo(ctx context.Context, jobID string, w io.Writer) error {
	resp, err := c.do(ctx, http.MethodGet, "/jobs/"+url.PathEscape(jobID)+"/results.csv", nil, nil, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, err = io.Copy(w, resp.Body)
	return err
}

// JobResultsCSV saves the job's CSV to outPath.
func (c *Client) JobResultsCSV(ctx context.Context, jobID, outPath string) error {
	f, err := os.Create(outPath)
	if err != nil {
		return err
	}
	if err := c.JobResultsCSVTo(ctx, jobID, f); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// ---------------------------------------------------------------- review

func (c *Client) ReviewQueue(ctx context.Context, q ReviewQuery) (*ReviewQueue, error) {
	v := url.Values{}
	for k, s := range map[string]string{"template_id": q.TemplateID, "job_id": q.JobID, "scan_id": q.ScanID, "name": q.Name, "kind": q.Kind} {
		if s != "" {
			v.Set(k, s)
		}
	}
	if q.Limit > 0 {
		v.Set("limit", strconv.Itoa(q.Limit))
	}
	if q.Offset > 0 {
		v.Set("offset", strconv.Itoa(q.Offset))
	}
	var out ReviewQueue
	return &out, c.doJSON(ctx, http.MethodGet, "/review", v, nil, "", &out)
}

// SubmitReview corrects values ({name: value}) and/or accepts reads as correct.
func (c *Client) SubmitReview(ctx context.Context, scanID string, corrections map[string]string, accept []string, reviewer string) (*ScanResult, error) {
	if corrections == nil {
		corrections = map[string]string{}
	}
	if accept == nil {
		accept = []string{}
	}
	body := map[string]interface{}{"corrections": corrections, "accept": accept}
	if reviewer != "" {
		body["reviewer"] = reviewer
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	var out ScanResult
	return &out, c.doJSON(ctx, http.MethodPost, "/scans/"+url.PathEscape(scanID)+"/review", nil, bytes.NewReader(raw), "application/json", &out)
}
