//go:build windows

package main

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
)

func TestResolveNestedCaseAndRejectAmbiguity(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Query().Get("directory") {
		case "":
			json.NewEncoder(w).Encode([]entry{{Name: "Фото", Type: "directory"}})
		case "Фото":
			json.NewEncoder(w).Encode([]entry{{Name: "Summer", Type: "directory"}})
		case "Фото/Summer":
			json.NewEncoder(w).Encode([]entry{{Name: "Mixed.txt", Type: "file"}, {Name: "dup", Type: "file"}, {Name: "DUP", Type: "file"}})
		default:
			t.Errorf("non-canonical directory %q", r.URL.RawQuery)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	client, _ := newAPIClient(profile{ServerURL: server.URL}, "token", "")
	cloud := newCloudFileSystem(client, "space", t.TempDir())
	canonical, _, err := cloud.resolve("фото/SUMMER/mIXED.TXT", false)
	if err != nil || canonical != "Фото/Summer/Mixed.txt" {
		t.Fatalf("%s: %v", canonical, err)
	}
	canonical, info, err := cloud.resolve("ФОТО/summer/New.txt", true)
	if err != nil || info != nil || canonical != "Фото/Summer/New.txt" {
		t.Fatalf("create: %s %v", canonical, err)
	}
	if _, _, err := cloud.resolve("Фото/Summer/dup", false); !errors.Is(err, os.ErrExist) {
		t.Fatalf("ambiguous file: %v", err)
	}
}

func TestEmptyDirectoryContract(t *testing.T) {
	dir := newDirectoryHandle(&remoteInfo{directory: true}, nil)
	if entries, err := dir.Readdir(-1); err != nil || len(entries) != 0 {
		t.Fatalf("read all: %v", err)
	}
	if _, err := dir.Readdir(1); err != io.EOF {
		t.Fatalf("read one: %v", err)
	}
}

func TestReadOnlyHandleDoesNotFlush(t *testing.T) {
	path := filepath.Join(t.TempDir(), "readonly.txt")
	if err := os.WriteFile(path, []byte("test"), 0600); err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	tracked := &trackedFile{File: f}
	if err := tracked.Sync(); err != nil {
		t.Fatal(err)
	}
	if err := tracked.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestConstrainedWriteNeverExtendsOrPanics(t *testing.T) {
	f, err := os.CreateTemp(t.TempDir(), "write")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	f.Write([]byte("12345"))
	tracked := &trackedFile{File: f}
	if n, err := tracked.ConstrainedWriteAt([]byte("abcdef"), 3); n != 2 || err != nil {
		t.Fatalf("%d %v", n, err)
	}
	if n, err := tracked.ConstrainedWriteAt([]byte("z"), 5); n != 0 || err != nil {
		t.Fatalf("%d %v", n, err)
	}
	actual, _ := os.ReadFile(f.Name())
	if string(actual) != "123ab" {
		t.Fatal(string(actual))
	}
}

func TestNormalizeName(t *testing.T) {
	tests := map[string]string{
		`\Фото\Отпуск\море.jpg`: "Фото/Отпуск/море.jpg",
		"/":                     "",
		"Документы/report.txt":  "Документы/report.txt",
	}
	for input, expected := range tests {
		actual, err := normalizeName(input)
		if err != nil || actual != expected {
			t.Fatalf("normalizeName(%q) = %q, %v; want %q", input, actual, err, expected)
		}
	}
	if _, err := normalizeName("../secret"); err == nil {
		t.Fatal("path traversal was accepted")
	}
}

func TestRouteURLPreservesUnicodePathAndQuery(t *testing.T) {
	base, err := url.Parse("https://cloud.example:8766")
	if err != nil {
		t.Fatal(err)
	}
	client := &apiClient{baseURL: base}
	logical := "Фото/Лето 2026/море #1.jpg"
	target, err := client.routeURL(fileRoute("space-1", logical))
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(target)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Path != "/v1/spaces/space-1/files/Фото/Лето 2026/море #1.jpg" {
		t.Fatalf("unexpected decoded path: %q", parsed.Path)
	}
	if parsed.RawQuery != "" {
		t.Fatalf("unexpected query: %q", parsed.RawQuery)
	}

	listing, err := client.routeURL("/v1/spaces/space-1/entries?directory=" + url.QueryEscape("Фото/Лето 2026"))
	if err != nil {
		t.Fatal(err)
	}
	parsed, _ = url.Parse(listing)
	if parsed.Query().Get("directory") != "Фото/Лето 2026" {
		t.Fatalf("directory query was corrupted: %q", parsed.Query().Get("directory"))
	}
}

func TestAPIRouteRejectsAbsoluteURL(t *testing.T) {
	base, _ := url.Parse("https://cloud.example")
	client := &apiClient{baseURL: base}
	if _, err := client.routeURL("https://attacker.example/file"); err == nil {
		t.Fatal("absolute API route was accepted")
	}
}

func TestAPIClientDoesNotFollowRedirects(t *testing.T) {
	client, err := newAPIClient(
		profile{ServerURL: "https://cloud.example"},
		"token",
		"",
	)
	if err != nil {
		t.Fatal(err)
	}
	request, _ := http.NewRequest(http.MethodGet, "https://other.example/file", nil)
	if err := client.http.CheckRedirect(request, nil); !errors.Is(err, http.ErrUseLastResponse) {
		t.Fatalf("unexpected redirect policy: %v", err)
	}
}

func TestZrokHeaderOnAPIUploadAndDownload(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("skip_zrok_interstitial") != "1" {
			t.Error("missing zrok API header")
		}
		if r.Header.Get("Authorization") != "Bearer test-token" {
			t.Error("missing device authentication")
		}
		_, _ = w.Write([]byte("test payload"))
	}))
	defer server.Close()
	client, err := newAPIClient(profile{ServerURL: server.URL}, "test-token", "")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.request(http.MethodGet, "/v1/health", nil, ""); err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(t.TempDir(), "download.bin")
	if err := client.download("space", "file.bin", "", file); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(file); err != nil || string(data) != "test payload" {
		t.Fatalf("invalid download: %v", err)
	}
	if err := client.upload("space", "file.bin", file); err != nil {
		t.Fatal(err)
	}
}
