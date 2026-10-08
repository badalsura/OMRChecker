package omrclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"time"
)

// ---------------------------------------------------------------- results

// ResultsQuery filters GET /results. View: all, flagged, unflagged, reviewed,
// not_reviewed, verified, corrected, errors.
type ResultsQuery struct {
	TemplateID, JobID, Status, View, Name, Flag, File, Order string
	Limit, Offset                                            int
}

// ResultRow is one graded sheet as listed by the index.
type ResultRow struct {
	ID         string   `json:"id"`
	TemplateID string   `json:"template_id"`
	JobID      string   `json:"job_id"`
	FileName   string   `json:"file_name"`
	Status     string   `json:"status"`
	Score      *float64 `json:"score"`
	FlagCount  int      `json:"flag_count"`
	Verified   int      `json:"verified"`
	Corrected  int      `json:"corrected"`
}

// ResultsPage is a page of GET /results.
type ResultsPage struct {
	Items []ResultRow `json:"items"`
	Total int         `json:"total"`
}

func (c *Client) ListResults(ctx context.Context, q ResultsQuery) (*ResultsPage, error) {
	v := url.Values{}
	for k, s := range map[string]string{"template_id": q.TemplateID, "job_id": q.JobID, "status": q.Status, "view": q.View, "name": q.Name, "flag": q.Flag, "file": q.File, "order": q.Order} {
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
	var out ResultsPage
	return &out, c.doJSON(ctx, http.MethodGet, "/results", v, nil, "", &out)
}

// Render re-reads a sheet and returns its overlay (fields with bubble boxes,
// zones, outputs, checks) plus image_url, width and height.
func (c *Client) Render(ctx context.Context, scanID string) (map[string]interface{}, error) {
	var out map[string]interface{}
	return out, c.doJSON(ctx, http.MethodGet, "/scans/"+url.PathEscape(scanID)+"/render", nil, nil, "", &out)
}

// Toggle flips one bubble of a field.
type Toggle struct {
	Field string `json:"field"`
	Value string `json:"value"`
}

func (c *Client) postJSON(ctx context.Context, method, path string, body interface{}, out interface{}) error {
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	return c.doJSON(ctx, method, path, nil, bytes.NewReader(raw), "application/json", out)
}

// Correct changes values ({name: value}) and/or toggles bubbles; the server
// recomputes field values, outputs, score and status, and audits the change.
func (c *Client) Correct(ctx context.Context, scanID string, changes map[string]string, toggles []Toggle) (map[string]interface{}, error) {
	if changes == nil {
		changes = map[string]string{}
	}
	if toggles == nil {
		toggles = []Toggle{}
	}
	var out map[string]interface{}
	return out, c.postJSON(ctx, http.MethodPost, "/scans/"+url.PathEscape(scanID)+"/corrections", map[string]interface{}{"changes": changes, "toggle": toggles}, &out)
}

// Verify marks every value of a sheet as checked by a person.
func (c *Client) Verify(ctx context.Context, scanID string) (map[string]interface{}, error) {
	var out map[string]interface{}
	return out, c.postJSON(ctx, http.MethodPost, "/scans/"+url.PathEscape(scanID)+"/verify", map[string]interface{}{}, &out)
}

// Accuracy reports how often auto-accepted fields needed no correction.
func (c *Client) Accuracy(ctx context.Context, templateID, jobID string) (map[string]interface{}, error) {
	v := url.Values{}
	if templateID != "" {
		v.Set("template_id", templateID)
	}
	if jobID != "" {
		v.Set("job_id", jobID)
	}
	var out map[string]interface{}
	return out, c.doJSON(ctx, http.MethodGet, "/results/accuracy", v, nil, "", &out)
}

// ---------------------------------------------------------------- exports

// ExportRequest is the body of POST /exports. Format: csv, xlsx, pdf, sqlite, sql.
type ExportRequest struct {
	Format      string                 `json:"format"`
	Filters     map[string]interface{} `json:"filters"`
	Profile     map[string]interface{} `json:"profile,omitempty"`
	ProfileName string                 `json:"profile_name,omitempty"`
	SQLURL      string                 `json:"sql_url,omitempty"`
	Wait        bool                   `json:"wait"`
}

// Export is an export's status record.
type Export struct {
	ID          string   `json:"id"`
	Format      string   `json:"format"`
	State       string   `json:"state"`
	Rows        int      `json:"rows"`
	Total       int      `json:"total"`
	Warnings    []string `json:"warnings"`
	Error       string   `json:"error"`
	FileName    string   `json:"file_name"`
	DownloadURL string   `json:"download_url"`
}

func (c *Client) CreateExport(ctx context.Context, r ExportRequest) (*Export, error) {
	if r.Filters == nil {
		r.Filters = map[string]interface{}{}
	}
	var out Export
	return &out, c.postJSON(ctx, http.MethodPost, "/exports", r, &out)
}

func (c *Client) ExportStatus(ctx context.Context, exportID string) (*Export, error) {
	var out Export
	return &out, c.doJSON(ctx, http.MethodGet, "/exports/"+url.PathEscape(exportID), nil, nil, "", &out)
}

// WaitForExport polls until the export completed or failed (an error).
func (c *Client) WaitForExport(ctx context.Context, exportID string, poll time.Duration) (*Export, error) {
	for {
		e, err := c.ExportStatus(ctx, exportID)
		if err != nil {
			return nil, err
		}
		if e.State == "failed" {
			return e, fmt.Errorf("export failed: %s", e.Error)
		}
		if e.State == "completed" {
			return e, nil
		}
		select {
		case <-ctx.Done():
			return e, ctx.Err()
		case <-time.After(poll):
		}
	}
}

// DownloadExport saves a completed export to outPath.
func (c *Client) DownloadExport(ctx context.Context, exportID, outPath string) error {
	resp, err := c.do(ctx, http.MethodGet, "/exports/"+url.PathEscape(exportID)+"/download", nil, nil, "")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	f, err := os.Create(outPath)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, resp.Body); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

// DeleteScan deletes one sheet's result and stored images (audited).
func (c *Client) DeleteScan(ctx context.Context, scanID string) (map[string]interface{}, error) {
	var out map[string]interface{}
	return out, c.doJSON(ctx, http.MethodDelete, "/results/"+url.PathEscape(scanID), nil, nil, "", &out)
}

// DeleteJob deletes a finished job and every result it produced (audited).
// Files in a server folder are never deleted.
func (c *Client) DeleteJob(ctx context.Context, jobID string) (map[string]interface{}, error) {
	var out map[string]interface{}
	return out, c.doJSON(ctx, http.MethodDelete, "/jobs/"+url.PathEscape(jobID), nil, nil, "", &out)
}

// ReviewCounts returns the pending total and per-name counts under the filters
// (template_id, job_id, scan_id, name, kind, since).
func (c *Client) ReviewCounts(ctx context.Context, filters map[string]string) (map[string]interface{}, error) {
	v := url.Values{}
	for k, val := range filters {
		if val != "" {
			v.Set(k, val)
		}
	}
	var out map[string]interface{}
	return out, c.doJSON(ctx, http.MethodGet, "/review/counts", v, nil, "", &out)
}

// AcceptReviewBulk accepts pending review items under the filters as read
// (template_id, job_id, scan_id, name, kind, expected, before, limit). Nothing
// is deleted; who and when are recorded per item.
func (c *Client) AcceptReviewBulk(ctx context.Context, filters map[string]interface{}) (map[string]interface{}, error) {
	if filters == nil {
		filters = map[string]interface{}{}
	}
	var out map[string]interface{}
	return out, c.postJSON(ctx, http.MethodPost, "/review/accept-bulk", filters, &out)
}

// CheckFolder reports whether a server folder can be read and how many
// images and PDFs it holds.
func (c *Client) CheckFolder(ctx context.Context, path string, recursive bool) (map[string]interface{}, error) {
	v := url.Values{}
	v.Set("path", path)
	v.Set("recursive", strconv.FormatBool(recursive))
	var out map[string]interface{}
	return out, c.doJSON(ctx, http.MethodGet, "/fs/check", v, nil, "", &out)
}
