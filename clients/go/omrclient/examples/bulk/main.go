// Bulk example: read a folder on the server's disk and save the CSV.
//
//	go run ./examples/bulk -url http://127.0.0.1:8000 -template exam -folder D:\scans\day1 -out day1.csv
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"github.com/omrchecker/omrclient"
)

func main() {
	base := flag.String("url", "http://127.0.0.1:8000", "API base URL")
	apiKey := flag.String("api-key", os.Getenv("OMR_API_KEY"), "X-API-Key")
	template := flag.String("template", "", "template id on the server, or a template.json path to upload")
	folder := flag.String("folder", "", "folder on the server to read")
	out := flag.String("out", "results.csv", "CSV output")
	workers := flag.Int("workers", 0, "worker processes (0 = server default)")
	flag.Parse()
	if *template == "" || *folder == "" {
		flag.Usage()
		os.Exit(2)
	}
	ctx := context.Background()
	c := omrclient.New(*base, *apiKey)

	templateID := *template
	if strings.HasSuffix(strings.ToLower(templateID), ".json") || strings.HasSuffix(strings.ToLower(templateID), ".zip") {
		tpl, err := c.UploadTemplate(ctx, "", []string{templateID})
		if err != nil {
			log.Fatal(err)
		}
		templateID = tpl.ID
		fmt.Println("uploaded template", templateID)
	}
	start := time.Now()
	job, err := c.CreateJob(ctx, omrclient.JobOptions{TemplateID: templateID, Folder: *folder, Workers: *workers})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("job %s: %d files\n", job.ID, job.TotalFiles)
	job, err = c.WaitForJob(ctx, job.ID, 2*time.Second, func(j *omrclient.Job) {
		rate := 0.0
		if j.ThroughputPerS != nil {
			rate = *j.ThroughputPerS * 60
		}
		fmt.Printf("\r%-10s %d/%d (%.0f/min) %v", j.State, j.ProcessedFiles, j.TotalFiles, rate, j.Counts)
	})
	fmt.Println()
	if err != nil {
		log.Fatal(err)
	}
	if err := c.JobResultsCSV(ctx, job.ID, *out); err != nil {
		log.Fatal(err)
	}
	elapsed := time.Since(start).Seconds()
	fmt.Printf("%s: %d files in %.1fs (%.0f sheets/min), %d review items, CSV -> %s\n",
		job.State, job.ProcessedFiles, elapsed, float64(job.ProcessedFiles)/elapsed*60, job.PendingReview, *out)
	if job.State != "completed" {
		os.Exit(1)
	}
}
